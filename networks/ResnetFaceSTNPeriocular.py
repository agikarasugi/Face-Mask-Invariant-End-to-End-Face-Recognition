from torch.nn import (Linear, Conv2d, BatchNorm1d, BatchNorm2d, PReLU, ReLU,
                      Sigmoid, Dropout2d, Dropout, AvgPool2d, MaxPool2d,
                      AdaptiveAvgPool2d, Sequential, Module, Parameter, Tanh)
import torch.nn.functional as F
import torch
import kornia
from collections import namedtuple


class Flatten(Module):
    def forward(self, input):
        return input.view(input.size(0), -1)


def l2_norm(input,axis=1):
    norm = torch.norm(input,2,axis,True)
    output = torch.div(input, norm)
    return output


class SEModule(Module):
    def __init__(self, channels, reduction):
        super(SEModule, self).__init__()
        self.avg_pool = AdaptiveAvgPool2d(1)
        self.fc1 = Conv2d(
            channels, channels // reduction, kernel_size=1,
            padding=0, bias=False)
        self.relu = ReLU(inplace=True)
        self.fc2 = Conv2d(
            channels // reduction, channels, kernel_size=1,
            padding=0, bias=False)
        self.sigmoid = Sigmoid()

    def forward(self, x):
        module_input = x
        x = self.avg_pool(x)
        x = self.fc1(x)
        x = self.relu(x)
        x = self.fc2(x)
        x = self.sigmoid(x)
        return module_input * x


class bottleneck_IR(Module):
    def __init__(self, in_channel, depth, stride):
        super(bottleneck_IR, self).__init__()
        if in_channel == depth:
            self.shortcut_layer = MaxPool2d(1, stride)
        else:
            self.shortcut_layer = Sequential(
                Conv2d(in_channel, depth, (1, 1), stride, bias=False),
                BatchNorm2d(depth))
        self.res_layer = Sequential(
            BatchNorm2d(in_channel),
            Conv2d(in_channel, depth, (3, 3), (1, 1), 1,bias=False),
            PReLU(depth),
            Conv2d(depth, depth, (3, 3), stride, 1, bias=False),
            BatchNorm2d(depth))

    def forward(self, x):
        shortcut = self.shortcut_layer(x)
        res = self.res_layer(x)
        return res + shortcut


class bottleneck_IR_SE(Module):
    def __init__(self, in_channel, depth, stride):
        super(bottleneck_IR_SE, self).__init__()
        if in_channel == depth:
            self.shortcut_layer = MaxPool2d(1, stride)
        else:
            self.shortcut_layer = Sequential(
                Conv2d(in_channel, depth, (1, 1), stride, bias=False),
                BatchNorm2d(depth))
        self.res_layer = Sequential(
            BatchNorm2d(in_channel),
            Conv2d(in_channel, depth, (3,3), (1,1),1, bias=False),
            PReLU(depth),
            Conv2d(depth, depth, (3,3), stride, 1, bias=False),
            BatchNorm2d(depth),
            SEModule(depth,16))

    def forward(self,x):
        shortcut = self.shortcut_layer(x)
        res = self.res_layer(x)
        return res + shortcut


class Bottleneck(namedtuple('Block', ['in_channel', 'depth', 'stride'])):
    '''A named tuple describing a ResNet block.'''


def get_block(in_channel, depth, num_units, stride = 2):
    return [Bottleneck(in_channel, depth, stride)] + [Bottleneck(depth, depth, 1) for i in range(num_units-1)]


def get_blocks(num_layers):
    if num_layers == 50:
        blocks = [
            get_block(in_channel=64, depth=64, num_units = 3),
            get_block(in_channel=64, depth=128, num_units=4),
            get_block(in_channel=128, depth=256, num_units=14),
            get_block(in_channel=256, depth=512, num_units=3)
        ]
    elif num_layers == 100:
        blocks = [
            get_block(in_channel=64, depth=64, num_units=3),
            get_block(in_channel=64, depth=128, num_units=13),
            get_block(in_channel=128, depth=256, num_units=30),
            get_block(in_channel=256, depth=512, num_units=3)
        ]
    elif num_layers == 152:
        blocks = [
            get_block(in_channel=64, depth=64, num_units=3),
            get_block(in_channel=64, depth=128, num_units=8),
            get_block(in_channel=128, depth=256, num_units=36),
            get_block(in_channel=256, depth=512, num_units=3)
        ]
    return blocks


class ResnetFaceSTNPeriocular(Module):
    def __init__(self, num_layers=50, drop_ratio=0.4, mode='ir_se'):
        super(ResnetFaceSTNPeriocular, self).__init__()
        assert num_layers in [50, 100, 152]
        assert mode in ['ir', 'ir_se']

        blocks = get_blocks(num_layers)
        if mode == 'ir':
            unit_module = bottleneck_IR
        elif mode == 'ir_se':
            unit_module = bottleneck_IR_SE

        self.locnet = Sequential(
            bottleneck_IR(3, 16, 2),
            bottleneck_IR(16, 32, 2),
            bottleneck_IR(32, 32, 2),
            bottleneck_IR(32, 64, 2),
            bottleneck_IR(64, 64, 1),
            torch.nn.AdaptiveAvgPool2d(1),
            Flatten(),
            Linear(64 * 1 * 1, 8),
        )


        self.input_layer = Sequential(Conv2d(3, 64, (3, 3), 1, 1, bias=False),
                                      BatchNorm2d(64),
                                      PReLU(64))
        self.output_layer = Sequential(BatchNorm2d(512),
                                       Dropout(drop_ratio),
                                       Flatten(),
                                       Linear(512 * 4 * 7, 512),
                                       BatchNorm1d(512))
        modules = []
        for block in blocks:
            for bottleneck in block:
                modules.append(
                    unit_module(bottleneck.in_channel,
                                bottleneck.depth,
                                bottleneck.stride))
        self.body = Sequential(*modules)
        
        self.locnet[7].weight.data.zero_()
        self.locnet[7].bias.data.copy_(torch.tensor([-1, -1, 
                                                      1, -1,
                                                      1,  1,
                                                     -1,  1],
                                                    dtype=torch.float32))
        
        self.register_buffer('points_dst',
                             torch.tensor([[[0, 0], [112 - 1, 0], [112 - 1, 64 - 1], [0, 64 - 1]]],
                                          requires_grad=False, dtype=torch.float32))
        
        self.h = 64
        self.w = 112
        
        
    def stn(self, x, ret_theta=False):
        points_src = self.locnet(x)
        points_src = torch.clamp(points_src, -1, 1)
        points_src = (points_src + 1.) * 63.5 # rescale to actual coordinate
        points_src = points_src.view(-1, 4, 2)
        
        B = points_src.shape[0]
        points_dst = self.points_dst.repeat(B, 1, 1)
        
        M = kornia.get_perspective_transform(points_src, points_dst)
        x = kornia.warp_perspective(x, M, dsize=(self.h, self.w))
        
        if ret_theta: return x, points_src
        return x

    
    def forward(self, x):
        x = self.stn(x)
        x = self.input_layer(x)
        x = self.body(x)
        x = self.output_layer(x)
        return l2_norm(x)
    