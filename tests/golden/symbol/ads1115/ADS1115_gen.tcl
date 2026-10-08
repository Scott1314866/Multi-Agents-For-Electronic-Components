# 由 Module_01/app/capture.py 生成 —— 请勿手改
proc step {msg} { puts ""; puts "\[STEP\] $msg"; flush stdout }
proc ok   {msg} { puts "   \[OK\] $msg"; flush stdout }
proc die  {msg} { puts "   \[FAIL\] $msg"; flush stdout; exit 1 }

set outDir  "D:/WorkSpace/Claude/Module_01/spike/s_web/ads1115/4019d6821da2"
set libPath "$outDir/ADS1115.OLB"
set dsnPath "$outDir/ADS1115.DSN"
file mkdir $outDir

step "load DBO DLL"
if {[catch {load C:/Cadence/SPB_17.2/tools/bin/orDb_Dll_Tcl64.dll DboTclWriteBasic} err]} { die "load: $err" }
set mSession [DboTclHelper_sCreateSession]

step "create .olb + symbol"
set st [DboState]
set mLib [$mSession CreateLib [DboTclHelper_sMakeCString $libPath] $st]
if {[$st Failed]} { die "CreateLib" }

set cName [DboTclHelper_sMakeCString "ADS1115"]
set mPkg  [$mLib NewPackage $cName $st]
set mCell [$mLib NewCell    $cName $st]
set mPart [$mLib NewPart    [DboTclHelper_sMakeCString "ADS1115.Normal"] $st]
if {[$st Failed]} { die "NewPackage/NewCell/NewPart" }
$mCell AddPart $mPart
$mPkg SetReferenceTemplate [DboTclHelper_sMakeCString "U"]
$mPart SetBoundingBox [DboTclHelper_sMakeCRect -50 50 50 -50]

step "draw body"
foreach c {
    {-50 50 50 50}
    {50 50 50 -50}
    {50 -50 -50 -50}
    {-50 -50 -50 50}
} {
    lassign $c x1 y1 x2 y2
    set s [DboState]
    $mPart NewLine $s [DboTclHelper_sMakeCPoint $x1 $y1] [DboTclHelper_sMakeCPoint $x2 $y2] 0 0
    if {[$s Failed]} { die "NewLine" }
}

set mDevice [$mPkg NewDevice [DboTclHelper_sMakeCString "U"] 0 $mCell $st]
if {[$st Failed]} { die "NewDevice" }

step "place 10 pins"
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "ADDR"] \
              4 \
              [DboTclHelper_sMakeCPoint -50 -40] \
              [DboTclHelper_sMakeCPoint -60 -40] 1 0]
if {[$s Failed]} { die "NewSymbolPinScalar(1)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "1"] [DboTclHelper_sMakeInt 0]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "ALERT/RDY"] \
              4 \
              [DboTclHelper_sMakeCPoint -50 -20] \
              [DboTclHelper_sMakeCPoint -60 -20] 1 1]
if {[$s Failed]} { die "NewSymbolPinScalar(2)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "2"] [DboTclHelper_sMakeInt 1]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "GND"] \
              4 \
              [DboTclHelper_sMakeCPoint -50 0] \
              [DboTclHelper_sMakeCPoint -60 0] 1 2]
if {[$s Failed]} { die "NewSymbolPinScalar(3)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "3"] [DboTclHelper_sMakeInt 2]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "AIN0"] \
              4 \
              [DboTclHelper_sMakeCPoint -50 20] \
              [DboTclHelper_sMakeCPoint -60 20] 1 3]
if {[$s Failed]} { die "NewSymbolPinScalar(4)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "4"] [DboTclHelper_sMakeInt 3]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "AIN1"] \
              4 \
              [DboTclHelper_sMakeCPoint -50 40] \
              [DboTclHelper_sMakeCPoint -60 40] 1 4]
if {[$s Failed]} { die "NewSymbolPinScalar(5)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "5"] [DboTclHelper_sMakeInt 4]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "SCL"] \
              4 \
              [DboTclHelper_sMakeCPoint 50 -40] \
              [DboTclHelper_sMakeCPoint 60 -40] 1 5]
if {[$s Failed]} { die "NewSymbolPinScalar(10)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "10"] [DboTclHelper_sMakeInt 5]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "SDA"] \
              4 \
              [DboTclHelper_sMakeCPoint 50 -20] \
              [DboTclHelper_sMakeCPoint 60 -20] 1 6]
if {[$s Failed]} { die "NewSymbolPinScalar(9)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "9"] [DboTclHelper_sMakeInt 6]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "VDD"] \
              4 \
              [DboTclHelper_sMakeCPoint 50 0] \
              [DboTclHelper_sMakeCPoint 60 0] 1 7]
if {[$s Failed]} { die "NewSymbolPinScalar(8)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "8"] [DboTclHelper_sMakeInt 7]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "AIN3"] \
              4 \
              [DboTclHelper_sMakeCPoint 50 20] \
              [DboTclHelper_sMakeCPoint 60 20] 1 8]
if {[$s Failed]} { die "NewSymbolPinScalar(7)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "7"] [DboTclHelper_sMakeInt 8]
set s [DboState]
set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "AIN2"] \
              4 \
              [DboTclHelper_sMakeCPoint 50 40] \
              [DboTclHelper_sMakeCPoint 60 40] 1 9]
if {[$s Failed]} { die "NewSymbolPinScalar(6)" }
$mPin SetIsLong 0
$mPin SetIsNumberVisible 1
$mDevice NewPinNumber [DboTclHelper_sMakeCString "6"] [DboTclHelper_sMakeInt 9]
$mLib SavePackageAll $mPkg
$mSession SaveLib $mLib
ok "10 pins, olb saved ($libPath)"

step "create .dsn and place the symbol"
set st [DboState]
set mDesign [$mSession CreateDesign $st \
    [DboTclHelper_sMakeCString "ADS1115"] [DboTclHelper_sMakeCString "ADS1115"]]
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
set mPkgRef [$mLib GetPackage [DboTclHelper_sMakeCString "ADS1115"] $st]
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
