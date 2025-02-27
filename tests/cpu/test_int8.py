from __future__ import division
from __future__ import print_function

import math
import os
import random
import unittest
import itertools
import time
import json
import sys

import torch
import torch.nn as nn
from torch.jit._recursive import wrap_cpp_module
import copy

import torch_ipex as ipex

import torch.nn as nn
from torch.nn import Parameter
import torch.nn.functional as F

from common_utils import TestCase

def get_rand_seed():
    return int(time.time() * 1000000000)

device = ipex.DEVICE
class TestQuantizationConfigueTune(TestCase):
    def test_quantization_status(self):
        x = torch.randn((4, 5), dtype=torch.float32).to(device)
        model = torch.nn.Linear(5, 10, bias=True).float().to(device)

        model1 = copy.deepcopy(model)
        x1 = x.clone()
        conf = ipex.AmpConf(torch.int8)
        with ipex.AutoMixPrecision(conf, running_mode='calibration'):
            ref = model1(x1)
        conf.save('configure.json')
        conf = ipex.AmpConf(torch.int8, 'configure.json')
        with ipex.AutoMixPrecision(conf, running_mode='inference'):
            y = model1(x1)
        self.assertTrue(ipex.core.is_int8_dil_tensor(y))
        jsonFile = open('configure.json', 'r')
        data = json.load(jsonFile)
        jsonFile.close()
        self.assertTrue(data[0]['quantized'])

        # check configure's change can works for calibration step,
        # we need use origin model, because after running inference
        # step, the model has beem quantized, after change quantized
        # to False, the output should be fp32, i.e. not be quantized.
        data[0]['quantized'] = False
        jsonFile = open('configure.json', "w+")
        jsonFile.write(json.dumps(data))
        jsonFile.close()
        # use user's changed configure.
        model2 = copy.deepcopy(model)
        x2 = x.clone()
        conf = ipex.AmpConf(torch.int8, 'configure.json')
        with ipex.AutoMixPrecision(conf, running_mode='calibration'):
            ref = model2(x2)
        conf.save('configure.json')
        conf = ipex.AmpConf(torch.int8, 'configure.json')
        jsonFile = open('configure.json', 'r')
        data = json.load(jsonFile)
        jsonFile.close()
        self.assertFalse(data[0]['quantized'])

        with ipex.AutoMixPrecision(conf, running_mode='inference'):
            y = model2(x2)
        self.assertTrue(ipex.core.is_fp32_dil_tensor(y))
        os.remove('configure.json')


class TestQuantization(TestCase):
    def _compare_fp32_int8(self, model, x):
        conf = ipex.AmpConf(torch.int8)
        with ipex.AutoMixPrecision(conf, running_mode='calibration'):
            ref = model(x)
        conf.save('configure.json')

        conf = ipex.AmpConf(torch.int8, 'configure.json')
        with ipex.AutoMixPrecision(conf, running_mode='inference'):
            y = model(x)

        self.assertTrue(ipex.core.is_int8_dil_tensor(y))
        self.assertEqual(ref, y, atol=1e-1, rtol=1e-5)
        os.remove('configure.json')

    def _lstm_compare_fp32_int8(self, model, *args):
        conf = ipex.AmpConf(torch.int8)
        with ipex.AutoMixPrecision(conf, running_mode='calibration'):
            with torch.no_grad():
                ref, hy_ref = model(*args)
        conf.save('configure.json')

        conf = ipex.AmpConf(torch.int8, 'configure.json')
        with ipex.AutoMixPrecision(conf, running_mode='inference'):
            with torch.no_grad():
                y, hy = model(*args)

        self.assertTrue(ipex.core.is_int8_dil_tensor(y))

        # self.assertEqual(ref, y, prec=0.1)
        self.assertEqual(ref, y, atol=0.1, rtol=1e-5)
        self.assertEqual(hy_ref[0], hy[0], atol=0.01, rtol=1e-5)
        self.assertEqual(hy_ref[1], hy[1], atol=0.01, rtol=1e-5)
        os.remove('configure.json')

    def test_conv2d(self):
        options = itertools.product([1, 4], [True, False], [1, 2])
        for groups, bias, dilation in options:
            N = torch.randint(3, 10, (1,)).item()
            C = torch.randint(1, 3, (1,)).item() * groups
            M = torch.randint(1, 3, (1,)).item() * groups
            x = torch.randn(N, C, 224, 224, dtype=torch.float32).to(device)
            conv2d = nn.Conv2d(in_channels=C,
                                     out_channels=M,
                                     kernel_size=3,
                                     stride=2,
                                     padding=1,
                                     dilation=dilation,
                                     bias=bias,
                                     groups=groups).float().to(device)
            self._compare_fp32_int8(conv2d, x)

    def test_relu(self):
        x = torch.randn((4, 5), dtype=torch.float32).to(device)
        relu = nn.ReLU()
        self._compare_fp32_int8(relu, x)

    def test_max_pool2d(self):
        N = torch.randint(3, 10, (1,)).item()
        C = torch.randint(3, 10, (1,)).item()
        for stride in [1, 2, 3]:
            for H, W in [(64, 64), (35, 39), (16, 19), [7, 8]]:
                for ceil_mode in [False, True]:
                    x = torch.randn(N, C, H, W, dtype=torch.float32).to(device)
                    max_pool2d = nn.MaxPool2d(kernel_size=3 if not ceil_mode else 7,
                                              stride=stride,
                                              padding=1,
                                              ceil_mode=ceil_mode)
                    self._compare_fp32_int8(max_pool2d, x)

    def test_avg_pool2d(self):
        N = torch.randint(3, 10, (1,)).item()
        C = torch.randint(3, 10, (1,)).item()

        for count_include_pad in [True, False]:
            x = torch.randn(N, C, 64, 64, dtype=torch.float32).to(device)
            avg_pool2d = torch.nn.AvgPool2d(
                kernel_size=3,
                stride=2,
                padding=1,
                count_include_pad=count_include_pad)
            self._compare_fp32_int8(avg_pool2d, x)

    def test_adaptive_avg_pool2d(self):
        N = torch.randint(3, 10, (1,)).item()
        C = torch.randint(3, 10, (1,)).item()
        x = torch.randn(N, C, 224, 224, dtype=torch.float32).to(device)

        adaptive_avg_pool2d = torch.nn.AdaptiveAvgPool2d(7)
        self._compare_fp32_int8(adaptive_avg_pool2d, x)

    def test_linear(self):
        in_features = torch.randint(3, 10, (1,)).item()
        out_features = torch.randint(3, 100, (1,)).item()

        for bias in [True, False]:
            x = torch.randn(3, in_features, dtype=torch.float32).to(device)
            linear = torch.nn.Linear(in_features, out_features, bias=bias).float().to(device)
            self._compare_fp32_int8(linear, x)

    def _lstm_int8(self, seq_len, batch_size, input_size, hidden_size, num_layers, bidirectional, bias, empty_state):
        rand_seed = int(get_rand_seed())

        print("{} rand sed: {}".format(sys._getframe().f_code.co_name, rand_seed))
        torch.manual_seed(rand_seed)

        num_directions = 2 if bidirectional else 1

        input_dpcpp = torch.FloatTensor(seq_len, batch_size, input_size).uniform_(-1, 1).to(device=device)
        h0_dpcpp = torch.FloatTensor(num_layers * num_directions, batch_size, hidden_size).uniform_(-1, 1).to(device=device)
        c0_dpcpp = torch.FloatTensor(num_layers * num_directions, batch_size, hidden_size).uniform_(-1, 1).to(device=device)
        model_dpcpp = torch.nn.LSTM(input_size=input_size, hidden_size=hidden_size, num_layers=num_layers, bidirectional=bidirectional, bias=bias).to(device=device).eval()

        self._lstm_compare_fp32_int8(model_dpcpp, input_dpcpp)

    def test_lstm(self):
        self._lstm_int8(seq_len=5, batch_size=2, input_size=16, hidden_size=16, num_layers=1, bidirectional=False, bias=True, empty_state=False)

        self._lstm_int8(seq_len=5, batch_size=2, input_size=16, hidden_size=16, num_layers=1, bidirectional=True, bias=True, empty_state=False)

        self._lstm_int8(seq_len=5, batch_size=2, input_size=16, hidden_size=16, num_layers=1, bidirectional=False, bias=False, empty_state=False)

        self._lstm_int8(seq_len=5, batch_size=2, input_size=16, hidden_size=16, num_layers=1, bidirectional=True, bias=False, empty_state=False)

if __name__ == '__main__':
    rand_seed = int(time.time() * 1000000000)
    torch.manual_seed(rand_seed)
    ipex.core.enable_auto_dnnl()
    test = unittest.main()
