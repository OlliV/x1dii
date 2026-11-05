# Live parameter map — X1D II 50C, fw 1.5.2

Captured 2026-11-05T06:33Z with `hbctl.py dump` (op 5 enumerate, op 2 read, op 6 describe).
87 parameters are advertised; `Descr` is the op-6 descriptor verbatim.

```
87 parameters

0x0056 StopDown                 = 0                            MLi0,1,1
0x0002 FocusModeParam           = 0                            LI2,0,1
0x0003 ExposureMode             = 1                            LI7,0,1,2,3,8,9,10
0x0004 MirroPosition            = 0                            MLi1,1,1
0x0006 LightmeterMode           = 2                            LI3,2,3,6
0x0007 EvAdjust                 = 0.0                          MLfc014000000000000,4014000000000000,3fd5555560000000
0x0008 Aperture                 = [40001]                      LR11,40001,30000a,38000a,44000a,80001,5f000a,b0001,d0001,100001,130001,160001
0x0009 Shutter                  = [20001]                      
0x000a Iso                      = 100.0                        LF10,bff0000000000000,4059000000000000,4069000000000000,4079000000000000,4089000000000000,4099000000000000,40a9000000000000,40b9000000000000,40c9000000000000,40d9000000000000
0x000e SensorType               = 15                           MLi0,20,1
0x000f None                     = '1.5.2'                      MLi0,0,0
0x0010 IrType                   = 5                            MLi0,20,1
0x0011 SensorUnitType           = 66                           MLi0,255,1
0x0012 SensorWidth              = 50                           MLi0,100,1
0x0013 SensorHeight             = 50                           MLi0,100,1
0x0016 SerialNumber             = 20200944                     MLi0,0,0
0x0017 BodyType                 = 150                          MLi0,255,1
0x001b FirmwareVersion          = '1.5.2'                      MLi0,0,0
0x001c Running                  = 1                            MLi0,1,1
0x0024 BackType                 = 66                           MLi0,255,1
0x0025 SensorUnitString         = 'Hasselblad X1D II 50C'      MLi0,0,0
0x0027 SerialString             = 'VQ20200944'                 MLi0,0,0
0x0028 RealBodyType             = 150                          MLi0,255,1
0x0029 MaxBlockSize             = 8388608                      MLi1,8388608,1
0x002b CalibrationBlockSize     = 1024                         MLi0,1024,1
0x002d CurrentImages            = 0                            MLr0,0,0
0x002e FocalLength              = 45                           MLi45,45,1
0x002f Battery                  = 95                           MLi0,100,1
0x0030 WhiteBalance             = 'I2,4724,30'                 MLi0,2,1
0x0033 TetheredCaptureEnabled   = 1                            MLi0,1,1
0x0034 Orientation              = 0                            MLi0,8,1
0x0035 LiveVideo                = None                         MLi0,2,1
0x003d LensMounted              = 1                            MLi0,1,1
0x003e ApertureName             = None                         LS34,4	4.8	5.6	6.8	8	9.5	11	13	16	19	22
0x003f ShutterName              = None                         LS232,1:08	48:16	34:08	24:08	17:04	12:04	8:32	6:02	4:16	3:01	2:08	1:31	1:04	45	32	23	16	11	8.0	6.0	4.0	3.0	2.0	1.4	T	B	1.0	0.7	0.5	0.3	1/4	1/6	1/8	1/10	1/15	1/20	1/30	1/45	1/60	1/90	1/125	1/180	1/250	1/350	1/500	1/750	1/1000	1/1500	1/2000
0x0041 DriveModeMode            = 0                            LI6,0,1,2,3,4,5
0x0043 CameraMode               = 0                            LI2,0,1
0x0044 VideoType                = 1                            LI2,0,1
0x0045 LiveviewTimeout          = None                         MLi0,1800,1
0x0046 WhiteBalanceCameraMode   = 2                            LI8,2,3,4,5,6,7,11,12
0x0047 None                     = 5000                         MLi2000,10000,1
0x0048 WhiteBalanceCameraTint   = 0                            MLi-100,100,1
0x0049 ExposuresLeft            = 12                           MLi0,2147483647,1
0x004c AutoIso                  = 100.0                        LF9,4059000000000000,4069000000000000,4079000000000000,4089000000000000,4099000000000000,40a9000000000000,40b9000000000000,40c9000000000000,40d9000000000000
0x004e Device0Status            = 0                            MLi0,5,1
0x004f Device1Status            = 0                            MLi0,5,1
0x0050 ZoomPoint                = 327685000                    MLi0,2147483647,1
0x0051 LiveVideoMode            = 'I14,0,1,2,3,4,5,6,7,8,9,10,11,12,13' MLi0,14,1
0x0052 FocusSizeList            = 'I3,44040656,66060985,88081312' MLi0,2147483647,1
0x0053 FocusPointAlignment      = 'I2,527044,524764'           MLi0,2147483647,1
0x0054 FocusPoint               = 330437504                    MLi0,2147483647,1
0x0055 FocusSize                = 88081312                     LI3,44040656,66060985,88081312
0x0056 StopDown                 = 0                            MLi0,1,1
0x0057 FlashEvAdjust            = 0.0                          MLfc008000000000000,4008000000000000,3fd5555560000000
0x0058 EShutter                 = 0                            MLi0,1,1
0x0059 FocusPositionPercent     = 1.0                          MLf0,3ff0000000000000,3f50624dd2f1a9fc
0x005a FocusDistanceScale       = 'I6,0,0,0,0,0,0'             MLi0,32767,1
0x005b None                     = 32767                        MLi0,32767,1
0x005c QuickExposureAdjust      = 0.0                          MLfc014000000000000,4014000000000000,3fd5555560000000
0x005d SelfTimerTime            = 3                            MLi2,60,1
0x005e SelfTimerLEDBlink        = 1                            MLi0,1,1
0x005f SelfTimerWhenFinished    = 1                            MLi0,1,1
0x0060 IntervalTime             = 6                            LI44,2,3,4,5,6,7,8,9,10,15,20,25,30,35,40,45,50,55,60,70,80,90,100,110,120,135,150,165,180,210,210,240,300,360,480,600,720,900,1200,1500,1800,2400,3000,3600
0x0061 IntervalFrames           = 10                           LI58,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,30,35,40,45,50,55,60,65,70,75,80,85,90,95,100,150,200,250,300,350,400,450,500,550,600,650,700,750,800,850,900,950,1000,-1
0x0062 IntervalInititalDelay    = 0                            LI5,0,2,10,60,255
0x0063 IntervalWhenFinished     = 0                            MLi0,1,1
0x0064 ExpBracketingAmount      = 1                            MLi0,5,1
0x0065 ExpBracketingFrames      = 1                            LI5,2,3,5,7,9
0x0066 None                     = 0                            LI6,0,2,5,10,30,60
0x0067 ExpBracketingParamInM    = 0                            MLi0,1,1
0x0068 ExpBracketingSequence    = 0                            MLi0,3,1
0x0069 None                     = 0                            MLi0,1,1
0x006a None                     = 2                            MLi0,4,1
0x006b FocusBracketingFrames    = 2                            LI57,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,30,35,40,45,50,55,60,65,70,75,80,85,90,95,100,150,200,250,300,350,400,450,500,550,600,650,700,750,800,850,900,950,1000
0x006c None                     = 0                            LI6,0,2,5,10,30,60
0x006d None                     = 125                          LI9,0,125,250,500,1000,2000,4000,8000,16000
0x006e None                     = 0                            MLi0,3,1
0x006f None                     = 0                            MLi0,1,1
0x0070 LensSerialNumber         = '16777215'                   MLi0,0,0
0x0071 LensShutterCount         = 1938                         MLi-1,2147483647,1
0x0072 WifiSsidName             = 'X1D II 50C CD7C75'          MLi0,0,0
0x0073 GpsStatus                = 1                            MLi0,2,1
0x0074 StorageMode              = 1                            MLi0,1,1
0x0075 PrimaryDeviceImageRaw    = 0                            MLi0,1,1
0x0076 Language                 = 0                            MLi0,1,1
0x0077 UnitOfDistance           = 0                            MLi0,1,1
0x0078 LensProductName          = 'XCD 45P'                    MLi0,0,0
```
