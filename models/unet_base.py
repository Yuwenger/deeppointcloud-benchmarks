import torch
from torch import nn
from abc import abstractmethod
import torch_geometric
from torch_geometric.nn import global_max_pool, global_mean_pool, fps, radius, knn_interpolate
from torch.nn import Sequential as Seq, Linear as Lin, ReLU, LeakyReLU, BatchNorm1d as BN, Dropout
from omegaconf.listconfig import ListConfig
from collections import defaultdict
from torch_geometric.nn import MessagePassing
from torch_geometric.nn.inits import reset

from datasets.base_dataset import BaseDataset
from .base_model import BaseModel

SPECIAL_NAMES = ['radius']


class BaseFactory:
    def __init__(self, module_name_down, module_name_up, modules_lib):
        self.module_name_down = module_name_down
        self.module_name_up = module_name_up
        self.modules_lib = modules_lib

    def get_module(self, index, flow):
        if flow.upper() == "UP":
            return getattr(self.modules_lib, self.module_name_up, None)
        else:
            return getattr(self.modules_lib, self.module_name_down, None)


class UnetBasedModel(BaseModel):
    """Create a Unet-based generator"""

    def _save_sampling_and_search(self, submodule, index):
        down_conv = submodule.down
        self._sampling_and_search_dict[index] = [
            getattr(down_conv, "sampler", None), getattr(down_conv, "neighbour_finder", None)]

    def __init__(self, opt, model_type, dataset: BaseDataset, modules_lib):
        """Construct a Unet generator
        Parameters:
            opt - options for the network generation
            model_type - type of the model to be generated
            num_class - output of the network
            modules_lib - all modules that can be used in the UNet
        We construct the U-Net from the innermost layer to the outermost layer.
        It is a recursive process.
        """
        super(UnetBasedModel, self).__init__(opt)

        num_convs = len(opt.down_conv.down_conv_nn)

        # Factory for creating up and down modules
        factory_module_cls = self._get_factory(model_type, modules_lib)
        down_conv_cls_name = opt.down_conv.module_name
        up_conv_cls_name = opt.up_conv.module_name
        self._factory_module = factory_module_cls(
            down_conv_cls_name, up_conv_cls_name, modules_lib)  # Create the factory object

        # construct unet structure
        contains_global = hasattr(opt, "innermost")
        if contains_global:
            assert len(opt.down_conv.down_conv_nn) + 1 == len(opt.up_conv.up_conv_nn)

            args_up = self._fetch_arguments_from_list(opt.up_conv, 0)
            args_up['up_conv_cls'] = self._factory_module.get_module(0, 'UP')

            unet_block = UnetSkipConnectionBlock(args_up=args_up, args_innermost=opt.innermost, modules_lib=modules_lib,
                                                 submodule=None, innermost=True)  # add the innermost layer
        else:
            unet_block = []

        if num_convs > 1:
            for index in range(num_convs - 1, 0, -1):
                args_up, args_down = self._fetch_arguments_up_and_down(opt, index, num_convs)
                unet_block = UnetSkipConnectionBlock(
                    args_up=args_up, args_down=args_down, submodule=unet_block)
                self._save_sampling_and_search(unet_block, index)
        else:
            index = num_convs

        index -= 1
        args_up, args_down = self._fetch_arguments_up_and_down(opt, index, num_convs)
        args_down['nb_feature'] = dataset.feature_dimension
        args_up['nb_feature'] = dataset.feature_dimension
        self.model = UnetSkipConnectionBlock(args_up=args_up, args_down=args_down, submodule=unet_block,
                                             outermost=True)  # add the outermost layer
        self._save_sampling_and_search(self.model, index)
        print(self)

    def _get_factory(self, model_name, modules_lib) -> BaseFactory:
        factory_module_cls = getattr(modules_lib, "{}Factory".format(model_name), None)
        if factory_module_cls is None:
            factory_module_cls = BaseFactory
        return factory_module_cls

    def _fetch_arguments_from_list(self, opt, index):
        args = {}
        for o, v in opt.items():
            name = str(o)
            if (isinstance(getattr(opt, o), ListConfig) and len(getattr(opt, o)) > 0):
                if name[-1] == 's' and name not in SPECIAL_NAMES:
                    name = name[:-1]
                v_index = v[index]
                if isinstance(v_index, ListConfig):
                    v_index = list(v_index)
                args[name] = v_index
            else:
                if isinstance(v, ListConfig):
                    v = list(v)
                args[name] = v
        args['index'] = index
        args['precompute_multi_scale'] = self._precompute_multi_scale
        return args

    def _fetch_arguments_up_and_down(self, opt, index, count_convs):
        # Defines down arguments
        args_down = self._fetch_arguments_from_list(opt.down_conv, index)
        args_down['down_conv_cls'] = self._factory_module.get_module(index, 'DOWN')

        # Defines up arguments
        args_up = self._fetch_arguments_from_list(opt.up_conv, count_convs - index)
        args_up['up_conv_cls'] = self._factory_module.get_module(index, 'UP')
        return args_up, args_down


class UnetSkipConnectionBlock(nn.Module):
    """Defines the Unet submodule with skip connection.
        X -------------------identity----------------------
        |-- downsampling -- |submodule| -- upsampling --|

    """

    def get_from_kwargs(self, kwargs, name):
        module = kwargs[name]
        kwargs.pop(name)
        return module

    def __init__(self, args_up=None, args_down=None, args_innermost=None, modules_lib=None, submodule=None, outermost=False, innermost=False):
        """Construct a Unet submodule with skip connections.
        Parameters:
            args_up -- arguments for up convs
            args_down -- arguments for down convs
            args_innermost -- arguments for innermost
            submodule (UnetSkipConnectionBlock) -- previously defined submodules
            outermost (bool)    -- if this module is the outermost module
            innermost (bool)    -- if this module is the innermost module
        """
        super(UnetSkipConnectionBlock, self).__init__()

        self.outermost = outermost
        self.innermost = innermost

        if innermost:
            assert outermost == False
            module_name = self.get_from_kwargs(args_innermost, 'module_name')
            inner_module_cls = getattr(modules_lib, module_name)
            self.inner = inner_module_cls(**args_innermost)
            upconv_cls = self.get_from_kwargs(args_up, 'up_conv_cls')
            self.up = upconv_cls(**args_up)
        else:
            downconv_cls = self.get_from_kwargs(args_down, 'down_conv_cls')
            upconv_cls = self.get_from_kwargs(args_up, 'up_conv_cls')

            downconv = downconv_cls(**args_down)
            upconv = upconv_cls(**args_up)

            self.down = downconv
            self.submodule = submodule
            self.up = upconv

    def forward(self, data):
        if self.innermost:
            data_out = self.inner(data)
            data = (data_out, data)
            return self.up(data)
        else:
            data_out = self.down(data)
            data_out2 = self.submodule(data_out)
            data = (data_out2, data)
            return self.up(data)
