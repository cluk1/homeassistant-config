#!/bin/bash

declare -A VALUES

PAIRS=(
)
VALUES["1"]=${PAIRS[@]}

PAIRS=(
"hmu;CurrentYieldPower"
"hmu;CurrentConsumedPower"
"hmu;EnergyIntegral"
"hmu;RunDataCompressorSpeed"
"hmu;RunDataStatuscode"
"vr_71;SensorData1"
"ctlv2;Hc1ActualFlowTempDesired"
"ctlv2;Hc1FlowTemp"
)
VALUES["5"]=${PAIRS[@]}

PAIRS=(
"hmu;YieldHcDay"
"hmu;YieldHwcDay"
"hmu;CompressorStarts"
"hmu;Fan1Starts"
)
VALUES["15"]=${PAIRS[@]}

PAIRS=(
"hmu;YieldHcMonth"
"hmu;YieldHwcMonth"
"hmu;YieldHc"
"hmu;YieldHwc"
"hmu;CopHcMonth"
"hmu;CopHwcMonth"
"hmu;CopHc"
"hmu;CopHwc"
)
VALUES["day"]=${PAIRS[@]}


if [ -z "${VALUES["$1"]}" ]; then
  echo "Fehler"
  exit 1
fi

for pair in ${VALUES["$1"]}; do
  keyval=($(echo $pair | tr ";" " "));
  circuit=${keyval[0]}
  value=${keyval[1]}
  ebusctl read -c $circuit $value > /dev/null
  sleep 1
done

