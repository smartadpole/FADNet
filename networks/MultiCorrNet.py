from __future__ import print_function
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from torch.autograd import Function
from torch.nn import init
from torch.nn.init import kaiming_normal
#from layers_package.resample2d_package.modules.resample2d import Resample2d
#from layers_package.channelnorm_package.modules.channelnorm import ChannelNorm
from correlation_package.modules.corr import Correlation1d # from PWC-Net
from networks.submodules import *

class MultiCorrNet(nn.Module):

    def __init__(self, ngpu, batchNorm=False, input_channel=3):
        super(MultiCorrNet, self).__init__()
        
        self.ngpu = ngpu
        self.batchNorm = batchNorm
        self.input_channel = input_channel

        # shrink and extract features
        self.conv1   = ResBlock(self.input_channel, 64, 2) # S/2
        self.conv2   = ResBlock(64, 128, 2) # S/4
        self.conv3   = ResBlock(128, 256, 2) # S/8
        self.conv4   = ResBlock(256, 512, stride=2) # S/16
        self.conv5   = ResBlock(512, 1024, stride=2) # S/32

        ## start corr from conv3, output channel is 32 + (max_disp * 2 / 2 + 1) 
	self.conv_redir = ResBlock(64, 64, stride=1)
	self.corr = Correlation1d(pad_size=40, kernel_size=1, max_displacement=40, stride1=1, stride2=2, corr_multiply=1)
	self.corr_activation = nn.LeakyReLU(0.1, inplace=True)

        self.pred_flow6 = predict_flow(1024)

        # iconv with deconv
        self.iconv5 = nn.ConvTranspose2d(1025, 512, 3, 1, 1)
        self.iconv4 = nn.ConvTranspose2d(769, 256, 3, 1, 1)
        self.iconv3 = nn.ConvTranspose2d(385, 128, 3, 1, 1)
        self.iconv2 = nn.ConvTranspose2d(193, 64, 3, 1, 1)
        self.iconv1 = nn.ConvTranspose2d(97, 32, 3, 1, 1)
        self.iconv0 = nn.ConvTranspose2d(17+self.input_channel, 16, 3, 1, 1)
        
        # expand and produce disparity
        self.upconv5 = deconv(1024, 512)
        self.upflow6to5 = nn.ConvTranspose2d(1, 1, 4, 2, 1, bias=False)
        self.pred_flow5 = predict_flow(512)

        self.upconv4 = deconv(512, 256)
        self.upflow5to4 = nn.ConvTranspose2d(1, 1, 4, 2, 1, bias=False)
        self.pred_flow4 = predict_flow(256)

        self.upconv3 = deconv(256, 128)
        self.upflow4to3 = nn.ConvTranspose2d(1, 1, 4, 2, 1, bias=False)
        self.pred_flow3 = predict_flow(128)

        self.upconv2 = deconv(128, 64)
        self.upflow3to2 = nn.ConvTranspose2d(1, 1, 4, 2, 1, bias=False)
        self.pred_flow2 = predict_flow(64)

        self.upconv1 = deconv(64, 32)
        self.upflow2to1 = nn.ConvTranspose2d(1, 1, 4, 2, 1, bias=False)
        self.pred_flow1 = predict_flow(32)

        self.upconv0 = deconv(32, 16)
        self.upflow1to0 = nn.ConvTranspose2d(1, 1, 4, 2, 1, bias=False)
        self.pred_flow0 = predict_flow(16)

        # weight initialization
        for m in self.modules():
            if isinstance(m, nn.Conv2d) or isinstance(m, nn.ConvTranspose2d):
                # n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                # m.weight.data.normal_(0, 0.02 / n)
                # m.weight.data.normal_(0, 0.02)
                kaiming_normal(m.weight.data)
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()
        
    def forward(self, input):

        # split left image and right image
        imgs = torch.chunk(input, 2, dim = 1)
        img_left = imgs[0]
        img_right = imgs[1]

        conv1_l = self.conv1(img_left)
        conv2_l = self.conv2(conv1_l)
        conv3a_l = self.conv3(conv2_l)

        conv1_r = self.conv1(img_right)
        conv2_r = self.conv2(conv1_r)
        conv3a_r = self.conv3(conv2_r)

        # Correlate corr3a_l and corr3a_r
        out_corr = self.corr(conv3a_l, conv3a_r)
        out_corr = self.corr_activation(out_corr)
	out_conv3a_redir = self.conv_redir(conv3a_l)
	in_conv3b = torch.cat((out_conv3a_redir, out_corr), 1)

        conv3b = self.conv3_1(in_conv3b)
        conv4a = self.conv4(conv3b)
        conv4b = self.conv4_1(conv4a)
        conv5a = self.conv5(conv4b)
        conv5b = self.conv5_1(conv5a)
        conv6a = self.conv6(conv5b)
        conv6b = self.conv6_1(conv6a)

        pr6 = self.pred_flow6(conv6b)
        upconv5 = self.upconv5(conv6b)
        upflow6 = self.upflow6to5(pr6)
        concat5 = torch.cat((upconv5, upflow6, conv5b), 1)
        iconv5 = self.iconv5(concat5)

        pr5 = self.pred_flow5(iconv5)
        upconv4 = self.upconv4(iconv5)
        upflow5 = self.upflow5to4(pr5)
        concat4 = torch.cat((upconv4, upflow5, conv4b), 1)
        iconv4 = self.iconv4(concat4)
        
        pr4 = self.pred_flow4(iconv4)
        upconv3 = self.upconv3(iconv4)
        upflow4 = self.upflow4to3(pr4)
        concat3 = torch.cat((upconv3, upflow4, conv3b), 1)
        iconv3 = self.iconv3(concat3)

        pr3 = self.pred_flow3(iconv3)
        upconv2 = self.upconv2(iconv3)
        upflow3 = self.upflow3to2(pr3)
        concat2 = torch.cat((upconv2, upflow3, conv2_l), 1)
        iconv2 = self.iconv2(concat2)

        pr2 = self.pred_flow2(iconv2)
        upconv1 = self.upconv1(iconv2)
        upflow2 = self.upflow2to1(pr2)
        concat1 = torch.cat((upconv1, upflow2, conv1_l), 1)
        iconv1 = self.iconv1(concat1)

        pr1 = self.pred_flow1(iconv1)
        upconv0 = self.upconv0(iconv1)
        upflow1 = self.upflow1to0(pr1)
        concat0 = torch.cat((upconv0, upflow1, img_left), 1)
        iconv0 = self.iconv0(concat0)

        # predict flow
        pr0 = self.pred_flow0(iconv0)

        # predict flow from dropout output
        # pr6 = self.pred_flow6(F.dropout2d(conv6b))
        # pr5 = self.pred_flow5(F.dropout2d(iconv5))
        # pr4 = self.pred_flow4(F.dropout2d(iconv4))
        # pr3 = self.pred_flow3(F.dropout2d(iconv3))
        # pr2 = self.pred_flow2(F.dropout2d(iconv2))
        # pr1 = self.pred_flow1(F.dropout2d(iconv1))
        # pr0 = self.pred_flow0(F.dropout2d(iconv0))

        # if self.training:
        #     # print("finish forwarding.")
        #     return pr0, pr1, pr2, pr3, pr4, pr5, pr6
        # else:
        #     return pr0

        # can be chosen outside
        return pr0, pr1, pr2, pr3, pr4, pr5, pr6

    def weight_parameters(self):
	return [param for name, param in self.named_parameters() if 'weight' in name]

    def bias_parameters(self):
	return [param for name, param in self.named_parameters() if 'bias' in name]

