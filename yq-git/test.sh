#!/bin/sh
oc apply -f yq-git-task.yml
oc create -f test-taskrun.yml

