#!/bin/sh
oc apply -f build/task.yml
oc create -f test-taskrun.yml

