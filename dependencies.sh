#!/bin/bash

GOPATH=${GOPATH:=$HOME/go}

echo "* Installing Python GRPC dependencies"
  sudo apt install python3-pip python3-setuptools python3-wheel
  pip3 -q install protobuf grpcio grpcio-tools requests
  # In case we 'make clean; sudo make test'
  sudo pip3 -q install --no-cache protobuf grpcio grpcio-tools requests

echo "* Installing go dependencies"
  sudo apt install golang
  mkdir -p $GOPATH
  export PATH=$PATH:$GOPATH/bin

echo "* Installing gnxi tools"
  repo=github.com/google/gnxi
  for tool in gnmi_{capabilities,get,set,target}; do
    go get $repo/$tool
    go install $repo/$tool
  done

echo "* Installing FAUCET as root"
  sudo pip3 -q install --no-cache --upgrade faucet

echo "* Done"
