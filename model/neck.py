from model.utils import *


class Neckv4(nn.Module):
    def __init__(self, output_ch):
        super().__init__()
        # upsampling
        self.conv7 = Conv(512, 256, 1, 1, 'leaky')
        self.up1 = nn.Upsample(scale_factor=2)

        # R 85
        self.conv8 = Conv(512, 256, 1, 1, 'leaky')

        # R -1 -3
        self.conv9 = C5(512, 256)

        # upsampling
        self.conv14 = Conv(256, 128, 1, 1, 'leaky')
        self.up2 = nn.Upsample(scale_factor=2)
        # R 54
        self.conv15 = Conv(256, 128, 1, 1, 'leaky')

        # R -1 -3
        self.conv16 = C5(256, 128)

        self.conv21 = Conv(128, 256, 3, 1, 'leaky')
        self.conv22 = Conv(256, output_ch, 1, 1, 'linear', bn=False, bias=True)

        # R -4
        self.conv23 = Conv(128, 256, 3, 2, 'leaky')

        # R -1 -16
        self.conv24 = C5(512, 256)

        self.conv29 = Conv(256, 512, 3, 1, 'leaky')
        self.conv30 = Conv(512, output_ch, 1, 1, 'linear', bn=False, bias=True)

        # R -4
        self.conv31 = Conv(256, 512, 3, 2, 'leaky')

        # R -1 -37
        self.conv32 = C5(1024, 512)

        self.conv37 = Conv(512, 1024, 3, 1, 'leaky')
        self.conv38 = Conv(1024, output_ch, 1, 1, 'linear', bn=False, bias=True)

    def forward(self, x1, x2, x3):
        # UP
        up1 = self.up1(self.conv7(x1))
        x2 = self.conv8(x2)
        # Fuse
        x2 = torch.cat([x2, up1], dim=1)

        x2 = self.conv9(x2)

        # UP
        up2 = self.up2(self.conv14(x2))
        x3 = self.conv15(x3)
        # Fuse
        x3 = torch.cat([x3, up2], dim=1)

        x3 = self.conv16(x3)

        # PAN
        x6 = self.conv22(self.conv21(x3))

        x3 = self.conv23(x3)

        x2 = torch.cat([x3, x2], dim=1)
        x2 = self.conv24(x2)

        x5 = self.conv30(self.conv29(x2))

        x2 = self.conv31(x2)

        x1 = torch.cat([x2, x1], dim=1)
        x1 = self.conv32(x1)

        x4 = self.conv38(self.conv37(x1))

        return x6, x5, x4
    

class Neckv5(nn.Module):
    def __init__(self, output_ch):
        super().__init__()
        # upsampling
        self.conv7 = Conv(1024, 512, 1, 1, 'leaky')
        self.up1 = nn.Upsample(scale_factor=2, mode='nearest')

        # csp
        self.csp1 = C3(1024, 512, 3, shortcut=False)

        # upsampling
        self.conv14 = Conv(512, 256, 1, 1, 'leaky')
        self.up2 = nn.Upsample(scale_factor=2, mode='nearest')

        # csp
        self.csp2 = C3(512, 256, 3, shortcut=False)

        self.conv15 = Conv(256, output_ch, 1, 1, 'linear', bn=False, bias=True)

        # R -1 -3
        self.conv16 = Conv(256, 256, 3, 2, 'leaky')
        self.csp3 = C3(512, 512, 3, shortcut=False)

        self.conv17 = Conv(512, output_ch, 1, 1, 'linear', bn=False, bias=True)

        self.conv18 = Conv(512, 512, 3, 2, 'leaky')
        self.csp4 = C3(1024, 1024, 3, shortcut=False)

        self.conv19 = Conv(1024, output_ch, 1, 1, 'linear', bn=False, bias=True)

    def forward(self, x1, x2, x3):
        x1 = self.conv7(x1)

        # UP
        up1 = self.up1(x1)
        # Fuse
        x2 = torch.cat([x2, up1], dim=1)

        x2 = self.csp1(x2)
        x2 = self.conv14(x2)

        # UP
        up2 = self.up2(x2)
        # Fuse
        x3 = torch.cat([x3, up2], dim=1)

        x3 = self.csp2(x3)

        x6 = self.conv15(x3)

        # PAN
        x3 = self.conv16(x3)
        x2 = torch.cat([x2, x3], dim=1)
        x2 = self.csp3(x2)

        x5 = self.conv17(x2)

        x2 = self.conv18(x2)
        x1 = torch.cat([x1, x2], dim=1)
        x1 = self.csp4(x1)

        x4 = self.conv19(x1)

        return x6, x5, x4
