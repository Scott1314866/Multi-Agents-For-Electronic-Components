# 由 Module_01/app/capture.py 生成 —— 请勿手改
proc step {msg} { puts ""; puts "\[STEP\] $msg"; flush stdout }
proc ok   {msg} { puts "   \[OK\] $msg"; flush stdout }
proc die  {msg} { puts "   \[FAIL\] $msg"; flush stdout; exit 1 }

set outDir  "D:/WorkSpace/Claude/Module_01/spike/s_stm32_web/702bb3679978"
set libPath "$outDir/STM32F105VCT6.OLB"
set dsnPath "$outDir/STM32F105VCT6.DSN"
file mkdir $outDir

step "load DBO DLL"
if {[catch {load C:/Cadence/SPB_17.2/tools/bin/orDb_Dll_Tcl64.dll DboTclWriteBasic} err]} { die "load: $err" }
set mSession [DboTclHelper_sCreateSession]

step "create .olb + symbol"
set st [DboState]
set mLib [$mSession CreateLib [DboTclHelper_sMakeCString $libPath] $st]
if {[$st Failed]} { die "CreateLib" }

set cName [DboTclHelper_sMakeCString "STM32F105VCT6"]
set mPkg  [$mLib NewPackage $cName $st]
set mCell [$mLib NewCell    $cName $st]
set mPart [$mLib NewPart    [DboTclHelper_sMakeCString "STM32F105VCT6.Normal"] $st]
if {[$st Failed]} { die "NewPackage/NewCell/NewPart" }
$mCell AddPart $mPart
$mPkg SetReferenceTemplate [DboTclHelper_sMakeCString "U"]
$mPart SetBoundingBox [DboTclHelper_sMakeCRect -260 250 260 -250]

step "draw body"
foreach c {
    {-260 250 260 250}
    {260 250 260 -250}
    {260 -250 -260 -250}
    {-260 -250 -260 250}
} {
    lassign $c x1 y1 x2 y2
    set s [DboState]
    $mPart NewLine $s [DboTclHelper_sMakeCPoint $x1 $y1] [DboTclHelper_sMakeCPoint $x2 $y2] 0 0
    if {[$s Failed]} { die "NewLine" }
}

set mDevice [$mPkg NewDevice [DboTclHelper_sMakeCString "U"] 0 $mCell $st]
if {[$st Failed]} { die "NewDevice" }

step "place 100 pins"
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PE2"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 -240] \
              [DboTclHelper_sMakeCPoint -270 -240] 1 0]
if {[$s Failed]} { die "NewSymbolPinScalar(1)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "1"] [DboTclHelper_sMakeInt 0]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PE3"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 -220] \
              [DboTclHelper_sMakeCPoint -270 -220] 1 1]
if {[$s Failed]} { die "NewSymbolPinScalar(2)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "2"] [DboTclHelper_sMakeInt 1]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PE4"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 -200] \
              [DboTclHelper_sMakeCPoint -270 -200] 1 2]
if {[$s Failed]} { die "NewSymbolPinScalar(3)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "3"] [DboTclHelper_sMakeInt 2]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PE5"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 -180] \
              [DboTclHelper_sMakeCPoint -270 -180] 1 3]
if {[$s Failed]} { die "NewSymbolPinScalar(4)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "4"] [DboTclHelper_sMakeInt 3]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PE6"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 -160] \
              [DboTclHelper_sMakeCPoint -270 -160] 1 4]
if {[$s Failed]} { die "NewSymbolPinScalar(5)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "5"] [DboTclHelper_sMakeInt 4]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "VBAT"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 -140] \
              [DboTclHelper_sMakeCPoint -270 -140] 1 5]
if {[$s Failed]} { die "NewSymbolPinScalar(6)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "6"] [DboTclHelper_sMakeInt 5]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PC13-TAMPER-RTC(5)"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 -120] \
              [DboTclHelper_sMakeCPoint -270 -120] 1 6]
if {[$s Failed]} { die "NewSymbolPinScalar(7)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "7"] [DboTclHelper_sMakeInt 6]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PC14-OSC32_IN(5)"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 -100] \
              [DboTclHelper_sMakeCPoint -270 -100] 1 7]
if {[$s Failed]} { die "NewSymbolPinScalar(8)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "8"] [DboTclHelper_sMakeInt 7]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PC15-OSC32_OUT(5)"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 -80] \
              [DboTclHelper_sMakeCPoint -270 -80] 1 8]
if {[$s Failed]} { die "NewSymbolPinScalar(9)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "9"] [DboTclHelper_sMakeInt 8]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "Vss_5"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 -60] \
              [DboTclHelper_sMakeCPoint -270 -60] 1 9]
if {[$s Failed]} { die "NewSymbolPinScalar(10)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "10"] [DboTclHelper_sMakeInt 9]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "VDD_5"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 -40] \
              [DboTclHelper_sMakeCPoint -270 -40] 1 10]
if {[$s Failed]} { die "NewSymbolPinScalar(11)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "11"] [DboTclHelper_sMakeInt 10]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "OSC_IN"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 -20] \
              [DboTclHelper_sMakeCPoint -270 -20] 1 11]
if {[$s Failed]} { die "NewSymbolPinScalar(12)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "12"] [DboTclHelper_sMakeInt 11]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "OSC_OUT"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 0] \
              [DboTclHelper_sMakeCPoint -270 0] 1 12]
if {[$s Failed]} { die "NewSymbolPinScalar(13)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "13"] [DboTclHelper_sMakeInt 12]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "NRST"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 20] \
              [DboTclHelper_sMakeCPoint -270 20] 1 13]
if {[$s Failed]} { die "NewSymbolPinScalar(14)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "14"] [DboTclHelper_sMakeInt 13]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PC0"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 40] \
              [DboTclHelper_sMakeCPoint -270 40] 1 14]
if {[$s Failed]} { die "NewSymbolPinScalar(15)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "15"] [DboTclHelper_sMakeInt 14]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PC1"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 60] \
              [DboTclHelper_sMakeCPoint -270 60] 1 15]
if {[$s Failed]} { die "NewSymbolPinScalar(16)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "16"] [DboTclHelper_sMakeInt 15]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PC2"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 80] \
              [DboTclHelper_sMakeCPoint -270 80] 1 16]
if {[$s Failed]} { die "NewSymbolPinScalar(17)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "17"] [DboTclHelper_sMakeInt 16]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PC3"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 100] \
              [DboTclHelper_sMakeCPoint -270 100] 1 17]
if {[$s Failed]} { die "NewSymbolPinScalar(18)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "18"] [DboTclHelper_sMakeInt 17]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "VSSA"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 120] \
              [DboTclHelper_sMakeCPoint -270 120] 1 18]
if {[$s Failed]} { die "NewSymbolPinScalar(19)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "19"] [DboTclHelper_sMakeInt 18]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "VREF-"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 140] \
              [DboTclHelper_sMakeCPoint -270 140] 1 19]
if {[$s Failed]} { die "NewSymbolPinScalar(20)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "20"] [DboTclHelper_sMakeInt 19]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "VREF+"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 160] \
              [DboTclHelper_sMakeCPoint -270 160] 1 20]
if {[$s Failed]} { die "NewSymbolPinScalar(21)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "21"] [DboTclHelper_sMakeInt 20]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "VDDA"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 180] \
              [DboTclHelper_sMakeCPoint -270 180] 1 21]
if {[$s Failed]} { die "NewSymbolPinScalar(22)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "22"] [DboTclHelper_sMakeInt 21]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PA0-WKUP"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 200] \
              [DboTclHelper_sMakeCPoint -270 200] 1 22]
if {[$s Failed]} { die "NewSymbolPinScalar(23)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "23"] [DboTclHelper_sMakeInt 22]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PA1"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 220] \
              [DboTclHelper_sMakeCPoint -270 220] 1 23]
if {[$s Failed]} { die "NewSymbolPinScalar(24)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "24"] [DboTclHelper_sMakeInt 23]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PA2"] \
              4 \
              [DboTclHelper_sMakeCPoint -260 240] \
              [DboTclHelper_sMakeCPoint -270 240] 1 24]
if {[$s Failed]} { die "NewSymbolPinScalar(25)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "25"] [DboTclHelper_sMakeInt 24]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "VDD_2"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 -240] \
              [DboTclHelper_sMakeCPoint 270 -240] 1 25]
if {[$s Failed]} { die "NewSymbolPinScalar(75)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "75"] [DboTclHelper_sMakeInt 25]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "Vss_2"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 -220] \
              [DboTclHelper_sMakeCPoint 270 -220] 1 26]
if {[$s Failed]} { die "NewSymbolPinScalar(74)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "74"] [DboTclHelper_sMakeInt 26]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "Not connected"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 -200] \
              [DboTclHelper_sMakeCPoint 270 -200] 1 27]
if {[$s Failed]} { die "NewSymbolPinScalar(73)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "73"] [DboTclHelper_sMakeInt 27]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PA13"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 -180] \
              [DboTclHelper_sMakeCPoint 270 -180] 1 28]
if {[$s Failed]} { die "NewSymbolPinScalar(72)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "72"] [DboTclHelper_sMakeInt 28]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PA12"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 -160] \
              [DboTclHelper_sMakeCPoint 270 -160] 1 29]
if {[$s Failed]} { die "NewSymbolPinScalar(71)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "71"] [DboTclHelper_sMakeInt 29]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PA11"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 -140] \
              [DboTclHelper_sMakeCPoint 270 -140] 1 30]
if {[$s Failed]} { die "NewSymbolPinScalar(70)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "70"] [DboTclHelper_sMakeInt 30]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PA10"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 -120] \
              [DboTclHelper_sMakeCPoint 270 -120] 1 31]
if {[$s Failed]} { die "NewSymbolPinScalar(69)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "69"] [DboTclHelper_sMakeInt 31]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PA9"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 -100] \
              [DboTclHelper_sMakeCPoint 270 -100] 1 32]
if {[$s Failed]} { die "NewSymbolPinScalar(68)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "68"] [DboTclHelper_sMakeInt 32]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PA8"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 -80] \
              [DboTclHelper_sMakeCPoint 270 -80] 1 33]
if {[$s Failed]} { die "NewSymbolPinScalar(67)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "67"] [DboTclHelper_sMakeInt 33]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PC9"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 -60] \
              [DboTclHelper_sMakeCPoint 270 -60] 1 34]
if {[$s Failed]} { die "NewSymbolPinScalar(66)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "66"] [DboTclHelper_sMakeInt 34]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PC8"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 -40] \
              [DboTclHelper_sMakeCPoint 270 -40] 1 35]
if {[$s Failed]} { die "NewSymbolPinScalar(65)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "65"] [DboTclHelper_sMakeInt 35]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PC7"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 -20] \
              [DboTclHelper_sMakeCPoint 270 -20] 1 36]
if {[$s Failed]} { die "NewSymbolPinScalar(64)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "64"] [DboTclHelper_sMakeInt 36]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PC6"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 0] \
              [DboTclHelper_sMakeCPoint 270 0] 1 37]
if {[$s Failed]} { die "NewSymbolPinScalar(63)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "63"] [DboTclHelper_sMakeInt 37]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PD15"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 20] \
              [DboTclHelper_sMakeCPoint 270 20] 1 38]
if {[$s Failed]} { die "NewSymbolPinScalar(62)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "62"] [DboTclHelper_sMakeInt 38]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PD14"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 40] \
              [DboTclHelper_sMakeCPoint 270 40] 1 39]
if {[$s Failed]} { die "NewSymbolPinScalar(61)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "61"] [DboTclHelper_sMakeInt 39]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PD13"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 60] \
              [DboTclHelper_sMakeCPoint 270 60] 1 40]
if {[$s Failed]} { die "NewSymbolPinScalar(60)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "60"] [DboTclHelper_sMakeInt 40]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PD12"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 80] \
              [DboTclHelper_sMakeCPoint 270 80] 1 41]
if {[$s Failed]} { die "NewSymbolPinScalar(59)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "59"] [DboTclHelper_sMakeInt 41]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PD11"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 100] \
              [DboTclHelper_sMakeCPoint 270 100] 1 42]
if {[$s Failed]} { die "NewSymbolPinScalar(58)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "58"] [DboTclHelper_sMakeInt 42]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PD10"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 120] \
              [DboTclHelper_sMakeCPoint 270 120] 1 43]
if {[$s Failed]} { die "NewSymbolPinScalar(57)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "57"] [DboTclHelper_sMakeInt 43]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PD9"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 140] \
              [DboTclHelper_sMakeCPoint 270 140] 1 44]
if {[$s Failed]} { die "NewSymbolPinScalar(56)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "56"] [DboTclHelper_sMakeInt 44]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PD8"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 160] \
              [DboTclHelper_sMakeCPoint 270 160] 1 45]
if {[$s Failed]} { die "NewSymbolPinScalar(55)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "55"] [DboTclHelper_sMakeInt 45]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PB15"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 180] \
              [DboTclHelper_sMakeCPoint 270 180] 1 46]
if {[$s Failed]} { die "NewSymbolPinScalar(54)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "54"] [DboTclHelper_sMakeInt 46]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PB14"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 200] \
              [DboTclHelper_sMakeCPoint 270 200] 1 47]
if {[$s Failed]} { die "NewSymbolPinScalar(53)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "53"] [DboTclHelper_sMakeInt 47]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PB13"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 220] \
              [DboTclHelper_sMakeCPoint 270 220] 1 48]
if {[$s Failed]} { die "NewSymbolPinScalar(52)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "52"] [DboTclHelper_sMakeInt 48]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PB12"] \
              4 \
              [DboTclHelper_sMakeCPoint 260 240] \
              [DboTclHelper_sMakeCPoint 270 240] 1 49]
if {[$s Failed]} { die "NewSymbolPinScalar(51)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "51"] [DboTclHelper_sMakeInt 49]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "VDD_3"] \
              4 \
              [DboTclHelper_sMakeCPoint -240 -250] \
              [DboTclHelper_sMakeCPoint -240 -260] 1 50]
if {[$s Failed]} { die "NewSymbolPinScalar(100)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "100"] [DboTclHelper_sMakeInt 50]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "Vss_3"] \
              4 \
              [DboTclHelper_sMakeCPoint -220 -250] \
              [DboTclHelper_sMakeCPoint -220 -260] 1 51]
if {[$s Failed]} { die "NewSymbolPinScalar(99)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "99"] [DboTclHelper_sMakeInt 51]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PE1"] \
              4 \
              [DboTclHelper_sMakeCPoint -200 -250] \
              [DboTclHelper_sMakeCPoint -200 -260] 1 52]
if {[$s Failed]} { die "NewSymbolPinScalar(98)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "98"] [DboTclHelper_sMakeInt 52]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PE0"] \
              4 \
              [DboTclHelper_sMakeCPoint -180 -250] \
              [DboTclHelper_sMakeCPoint -180 -260] 1 53]
if {[$s Failed]} { die "NewSymbolPinScalar(97)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "97"] [DboTclHelper_sMakeInt 53]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PB9"] \
              4 \
              [DboTclHelper_sMakeCPoint -160 -250] \
              [DboTclHelper_sMakeCPoint -160 -260] 1 54]
if {[$s Failed]} { die "NewSymbolPinScalar(96)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "96"] [DboTclHelper_sMakeInt 54]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PB8"] \
              4 \
              [DboTclHelper_sMakeCPoint -140 -250] \
              [DboTclHelper_sMakeCPoint -140 -260] 1 55]
if {[$s Failed]} { die "NewSymbolPinScalar(95)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "95"] [DboTclHelper_sMakeInt 55]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "BOOT0"] \
              4 \
              [DboTclHelper_sMakeCPoint -120 -250] \
              [DboTclHelper_sMakeCPoint -120 -260] 1 56]
if {[$s Failed]} { die "NewSymbolPinScalar(94)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "94"] [DboTclHelper_sMakeInt 56]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PB7"] \
              4 \
              [DboTclHelper_sMakeCPoint -100 -250] \
              [DboTclHelper_sMakeCPoint -100 -260] 1 57]
if {[$s Failed]} { die "NewSymbolPinScalar(93)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "93"] [DboTclHelper_sMakeInt 57]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PB6"] \
              4 \
              [DboTclHelper_sMakeCPoint -80 -250] \
              [DboTclHelper_sMakeCPoint -80 -260] 1 58]
if {[$s Failed]} { die "NewSymbolPinScalar(92)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "92"] [DboTclHelper_sMakeInt 58]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PB5"] \
              4 \
              [DboTclHelper_sMakeCPoint -60 -250] \
              [DboTclHelper_sMakeCPoint -60 -260] 1 59]
if {[$s Failed]} { die "NewSymbolPinScalar(91)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "91"] [DboTclHelper_sMakeInt 59]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PB4"] \
              4 \
              [DboTclHelper_sMakeCPoint -40 -250] \
              [DboTclHelper_sMakeCPoint -40 -260] 1 60]
if {[$s Failed]} { die "NewSymbolPinScalar(90)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "90"] [DboTclHelper_sMakeInt 60]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PB3"] \
              4 \
              [DboTclHelper_sMakeCPoint -20 -250] \
              [DboTclHelper_sMakeCPoint -20 -260] 1 61]
if {[$s Failed]} { die "NewSymbolPinScalar(89)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "89"] [DboTclHelper_sMakeInt 61]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PD7"] \
              4 \
              [DboTclHelper_sMakeCPoint 0 -250] \
              [DboTclHelper_sMakeCPoint 0 -260] 1 62]
if {[$s Failed]} { die "NewSymbolPinScalar(88)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "88"] [DboTclHelper_sMakeInt 62]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PD6"] \
              4 \
              [DboTclHelper_sMakeCPoint 20 -250] \
              [DboTclHelper_sMakeCPoint 20 -260] 1 63]
if {[$s Failed]} { die "NewSymbolPinScalar(87)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "87"] [DboTclHelper_sMakeInt 63]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PD5"] \
              4 \
              [DboTclHelper_sMakeCPoint 40 -250] \
              [DboTclHelper_sMakeCPoint 40 -260] 1 64]
if {[$s Failed]} { die "NewSymbolPinScalar(86)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "86"] [DboTclHelper_sMakeInt 64]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PD4"] \
              4 \
              [DboTclHelper_sMakeCPoint 60 -250] \
              [DboTclHelper_sMakeCPoint 60 -260] 1 65]
if {[$s Failed]} { die "NewSymbolPinScalar(85)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "85"] [DboTclHelper_sMakeInt 65]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PD3"] \
              4 \
              [DboTclHelper_sMakeCPoint 80 -250] \
              [DboTclHelper_sMakeCPoint 80 -260] 1 66]
if {[$s Failed]} { die "NewSymbolPinScalar(84)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "84"] [DboTclHelper_sMakeInt 66]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PD2"] \
              4 \
              [DboTclHelper_sMakeCPoint 100 -250] \
              [DboTclHelper_sMakeCPoint 100 -260] 1 67]
if {[$s Failed]} { die "NewSymbolPinScalar(83)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "83"] [DboTclHelper_sMakeInt 67]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PD1"] \
              4 \
              [DboTclHelper_sMakeCPoint 120 -250] \
              [DboTclHelper_sMakeCPoint 120 -260] 1 68]
if {[$s Failed]} { die "NewSymbolPinScalar(82)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "82"] [DboTclHelper_sMakeInt 68]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PD0"] \
              4 \
              [DboTclHelper_sMakeCPoint 140 -250] \
              [DboTclHelper_sMakeCPoint 140 -260] 1 69]
if {[$s Failed]} { die "NewSymbolPinScalar(81)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "81"] [DboTclHelper_sMakeInt 69]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PC12"] \
              4 \
              [DboTclHelper_sMakeCPoint 160 -250] \
              [DboTclHelper_sMakeCPoint 160 -260] 1 70]
if {[$s Failed]} { die "NewSymbolPinScalar(80)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "80"] [DboTclHelper_sMakeInt 70]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PC11"] \
              4 \
              [DboTclHelper_sMakeCPoint 180 -250] \
              [DboTclHelper_sMakeCPoint 180 -260] 1 71]
if {[$s Failed]} { die "NewSymbolPinScalar(79)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "79"] [DboTclHelper_sMakeInt 71]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PC10"] \
              4 \
              [DboTclHelper_sMakeCPoint 200 -250] \
              [DboTclHelper_sMakeCPoint 200 -260] 1 72]
if {[$s Failed]} { die "NewSymbolPinScalar(78)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "78"] [DboTclHelper_sMakeInt 72]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PA15"] \
              4 \
              [DboTclHelper_sMakeCPoint 220 -250] \
              [DboTclHelper_sMakeCPoint 220 -260] 1 73]
if {[$s Failed]} { die "NewSymbolPinScalar(77)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "77"] [DboTclHelper_sMakeInt 73]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PA14"] \
              4 \
              [DboTclHelper_sMakeCPoint 240 -250] \
              [DboTclHelper_sMakeCPoint 240 -260] 1 74]
if {[$s Failed]} { die "NewSymbolPinScalar(76)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "76"] [DboTclHelper_sMakeInt 74]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PA3"] \
              4 \
              [DboTclHelper_sMakeCPoint -240 250] \
              [DboTclHelper_sMakeCPoint -240 260] 1 75]
if {[$s Failed]} { die "NewSymbolPinScalar(26)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "26"] [DboTclHelper_sMakeInt 75]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "Vss_4"] \
              4 \
              [DboTclHelper_sMakeCPoint -220 250] \
              [DboTclHelper_sMakeCPoint -220 260] 1 76]
if {[$s Failed]} { die "NewSymbolPinScalar(27)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "27"] [DboTclHelper_sMakeInt 76]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "VDD_4"] \
              4 \
              [DboTclHelper_sMakeCPoint -200 250] \
              [DboTclHelper_sMakeCPoint -200 260] 1 77]
if {[$s Failed]} { die "NewSymbolPinScalar(28)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "28"] [DboTclHelper_sMakeInt 77]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PA4"] \
              4 \
              [DboTclHelper_sMakeCPoint -180 250] \
              [DboTclHelper_sMakeCPoint -180 260] 1 78]
if {[$s Failed]} { die "NewSymbolPinScalar(29)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "29"] [DboTclHelper_sMakeInt 78]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PA5"] \
              4 \
              [DboTclHelper_sMakeCPoint -160 250] \
              [DboTclHelper_sMakeCPoint -160 260] 1 79]
if {[$s Failed]} { die "NewSymbolPinScalar(30)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "30"] [DboTclHelper_sMakeInt 79]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PA6"] \
              4 \
              [DboTclHelper_sMakeCPoint -140 250] \
              [DboTclHelper_sMakeCPoint -140 260] 1 80]
if {[$s Failed]} { die "NewSymbolPinScalar(31)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "31"] [DboTclHelper_sMakeInt 80]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PA7"] \
              4 \
              [DboTclHelper_sMakeCPoint -120 250] \
              [DboTclHelper_sMakeCPoint -120 260] 1 81]
if {[$s Failed]} { die "NewSymbolPinScalar(32)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "32"] [DboTclHelper_sMakeInt 81]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PC4"] \
              4 \
              [DboTclHelper_sMakeCPoint -100 250] \
              [DboTclHelper_sMakeCPoint -100 260] 1 82]
if {[$s Failed]} { die "NewSymbolPinScalar(33)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "33"] [DboTclHelper_sMakeInt 82]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PC5"] \
              4 \
              [DboTclHelper_sMakeCPoint -80 250] \
              [DboTclHelper_sMakeCPoint -80 260] 1 83]
if {[$s Failed]} { die "NewSymbolPinScalar(34)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "34"] [DboTclHelper_sMakeInt 83]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PB0"] \
              4 \
              [DboTclHelper_sMakeCPoint -60 250] \
              [DboTclHelper_sMakeCPoint -60 260] 1 84]
if {[$s Failed]} { die "NewSymbolPinScalar(35)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "35"] [DboTclHelper_sMakeInt 84]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PB1"] \
              4 \
              [DboTclHelper_sMakeCPoint -40 250] \
              [DboTclHelper_sMakeCPoint -40 260] 1 85]
if {[$s Failed]} { die "NewSymbolPinScalar(36)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "36"] [DboTclHelper_sMakeInt 85]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PB2"] \
              4 \
              [DboTclHelper_sMakeCPoint -20 250] \
              [DboTclHelper_sMakeCPoint -20 260] 1 86]
if {[$s Failed]} { die "NewSymbolPinScalar(37)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "37"] [DboTclHelper_sMakeInt 86]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PE7"] \
              4 \
              [DboTclHelper_sMakeCPoint 0 250] \
              [DboTclHelper_sMakeCPoint 0 260] 1 87]
if {[$s Failed]} { die "NewSymbolPinScalar(38)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "38"] [DboTclHelper_sMakeInt 87]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PE8"] \
              4 \
              [DboTclHelper_sMakeCPoint 20 250] \
              [DboTclHelper_sMakeCPoint 20 260] 1 88]
if {[$s Failed]} { die "NewSymbolPinScalar(39)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "39"] [DboTclHelper_sMakeInt 88]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PE9"] \
              4 \
              [DboTclHelper_sMakeCPoint 40 250] \
              [DboTclHelper_sMakeCPoint 40 260] 1 89]
if {[$s Failed]} { die "NewSymbolPinScalar(40)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "40"] [DboTclHelper_sMakeInt 89]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PE10"] \
              4 \
              [DboTclHelper_sMakeCPoint 60 250] \
              [DboTclHelper_sMakeCPoint 60 260] 1 90]
if {[$s Failed]} { die "NewSymbolPinScalar(41)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "41"] [DboTclHelper_sMakeInt 90]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PE11"] \
              4 \
              [DboTclHelper_sMakeCPoint 80 250] \
              [DboTclHelper_sMakeCPoint 80 260] 1 91]
if {[$s Failed]} { die "NewSymbolPinScalar(42)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "42"] [DboTclHelper_sMakeInt 91]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PE12"] \
              4 \
              [DboTclHelper_sMakeCPoint 100 250] \
              [DboTclHelper_sMakeCPoint 100 260] 1 92]
if {[$s Failed]} { die "NewSymbolPinScalar(43)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "43"] [DboTclHelper_sMakeInt 92]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PE13"] \
              4 \
              [DboTclHelper_sMakeCPoint 120 250] \
              [DboTclHelper_sMakeCPoint 120 260] 1 93]
if {[$s Failed]} { die "NewSymbolPinScalar(44)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "44"] [DboTclHelper_sMakeInt 93]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PE14"] \
              4 \
              [DboTclHelper_sMakeCPoint 140 250] \
              [DboTclHelper_sMakeCPoint 140 260] 1 94]
if {[$s Failed]} { die "NewSymbolPinScalar(45)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "45"] [DboTclHelper_sMakeInt 94]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PE15"] \
              4 \
              [DboTclHelper_sMakeCPoint 160 250] \
              [DboTclHelper_sMakeCPoint 160 260] 1 95]
if {[$s Failed]} { die "NewSymbolPinScalar(46)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "46"] [DboTclHelper_sMakeInt 95]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PB10"] \
              4 \
              [DboTclHelper_sMakeCPoint 180 250] \
              [DboTclHelper_sMakeCPoint 180 260] 1 96]
if {[$s Failed]} { die "NewSymbolPinScalar(47)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "47"] [DboTclHelper_sMakeInt 96]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "PB11"] \
              4 \
              [DboTclHelper_sMakeCPoint 200 250] \
              [DboTclHelper_sMakeCPoint 200 260] 1 97]
if {[$s Failed]} { die "NewSymbolPinScalar(48)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "48"] [DboTclHelper_sMakeInt 97]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "Vss_1"] \
              4 \
              [DboTclHelper_sMakeCPoint 220 250] \
              [DboTclHelper_sMakeCPoint 220 260] 1 98]
if {[$s Failed]} { die "NewSymbolPinScalar(49)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "49"] [DboTclHelper_sMakeInt 98]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "VDD_1"] \
              4 \
              [DboTclHelper_sMakeCPoint 240 250] \
              [DboTclHelper_sMakeCPoint 240 260] 1 99]
if {[$s Failed]} { die "NewSymbolPinScalar(50)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "50"] [DboTclHelper_sMakeInt 99]
$mLib SavePackageAll $mPkg
$mSession SaveLib $mLib
ok "100 pins, olb saved ($libPath)"

step "create .dsn and place the symbol"
set st [DboState]
set mDesign [$mSession CreateDesign $st \
    [DboTclHelper_sMakeCString "STM32F105VCT6"] [DboTclHelper_sMakeCString "STM32F105VCT6"]]
if {[$st Failed]} { die "CreateDesign" }

set st [DboState]
set mRoot [$mDesign GetRootSchematic $st]
set st [DboState]
set mPage [$mRoot NewPage $st [DboTclHelper_sMakeCString "PAGE1"] 1]
if {[$st Failed]} { die "NewPage" }

set st [DboState]
set pIt [$mLib NewPartsIter $st]
set st [DboState]
set mPartRef [$pIt NextPart $st]
set st [DboState]
set mPkgRef [$mLib GetPackage [DboTclHelper_sMakeCString "STM32F105VCT6"] $st]
set st [DboState]
set dIt [$mPkgRef NewDevicesIter $st]
set st [DboState]
set mDevRef [$dIt NextDevice $st]
if {$mPartRef eq "NULL" || $mDevRef eq "NULL"} { die "cannot fetch part/device from lib" }

set s [DboState]
set r [$mPage NewPlacedInst $s \
    [DboTclHelper_sMakeCString "U1"] $cName $mPartRef $mDevRef \
    [DboTclHelper_sMakeCPoint 300 400]]
if {[$s Failed]} { die "NewPlacedInst" }

step "save .dsn"
set st [DboState]
$mSession SaveDesignAs $mDesign 2 0 [DboTclHelper_sMakeCString $dsnPath] 1
if {[$st Failed]} { die "SaveDesignAs" }
if {![file exists $dsnPath]} { die "dsn not created" }

puts ""
puts "===== CAPTURE RESULT: PASS ====="
puts "  olb : $libPath"
puts "  dsn : $dsnPath"
exit 0
