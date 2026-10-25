#!/bin/bash

# Container-Name ohne festes Präfix suchen (früher addon_, jetzt app_)
container=$(docker ps --format '{{.Names}}' | grep -E '^(addon|app)_.*_ebus')
if [ ! -z "$container" ]; then
  docker exec $container bash /config/ebusd/readimportant.sh $*
fi
