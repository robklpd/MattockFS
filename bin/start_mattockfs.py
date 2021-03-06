#!/usr/bin/python
import mattock
import os
import json
def get_instance_count():
    try:
        json_data = open("/etc/mattockfs.json").read()
        data = json.loads(json_data)
        return data["instance_count"]
    except:
        print "No config file found, running two default instances"
        return 2

for instance in range(1,get_instance_count()):
    newpid=os.fork()
    if newpid == 0:
        mattock.run(str(instance))
        os._exit(0)
mattock.run()
