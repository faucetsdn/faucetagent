#!/bin/bash
set -e

PIP3='pip3 -q'
APT='apt -qq -y'

echo "* Installing python dependencies"
  sudo $APT install python3-pip python3-setuptools python3-wheel psmisc
  $PIP3 install protobuf grpcio grpcio-tools requests prometheus_client

echo "* Done"
