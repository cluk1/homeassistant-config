#!/bin/bash

container=$(docker ps --format '{{.Names}}' | grep addon_.*_ebus)
if [ ! -z "$container" ]; then
  docker exec $container bash /config/ebusd/readimportant.sh $*
fi
