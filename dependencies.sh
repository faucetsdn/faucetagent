#!/bin/bash -x

echo "* Installing Python GRPC dependencies"
  sudo apt install python3-pip python3-setuptools python3-wheel
  pip3 install protobuf grpcio grpcio-tools
  # In case we 'make clean; sudo make test'
  sudo -H pip3 install protobuf grpcio grpcio-tools

echo "* Installing go dependencies"
  sudo apt install golang
  export GOPATH=$HOME/go
  mkdir -p $GOPATH
  export PATH=$PATH:$GOPATH/bin

echo "* Installing gnxi tools"
  repo=github.com/google/gnxi
  for tool in gnmi_{capabilities,get,set,target}; do
    go get $repo/$tool
    go install $repo/$tool
  done

echo "* Installing FAUCET as root"
sudo -H pip3 install faucet
