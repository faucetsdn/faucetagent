#!/bin/bash
set -e

PIP3='sudo pip3 install -q -U'
APT='sudo apt -qq -y install'

echo "* Installing apt dependencies"
  $APT iputils-arping
  # Workaround to avoid breaking go on github actions
  which go || $APT install golang

echo "* Checking for go version 1.7 or later"
  goversion=$(go version | awk '{print $3;}')
  latest=`printf "go1.7\n%s" $goversion | sort -V | tail -n1`
  if [ "$latest" != "$goversion" ]; then
      echo "gnxi requires go version >= go1.7 (found $goversion)"
      exit 1
  fi

echo "* Installing gnxi tools"
  if [ "$GOPATH" == "" ]; then
    export GOPATH=$HOME/go
    echo "* GOPATH not set - using $GOPATH"
  fi
  mkdir -p $GOPATH
  export PATH=$GOPATH/bin:$PATH
  repo=github.com/google/gnxi
  for tool in gnmi_{capabilities,get,set,target}; do
    go get $repo/$tool
    go install $repo/$tool
  done

echo "* Installing python dependencies and FAUCET"
  $PIP3 -r test-requirements.txt

  $APT openvswitch-switch net-tools telnet
  sudo service openvswitch-switch start
  TMPDIR=$(mktemp -d) && pushd $TMPDIR
  git clone https://github.com/mininet/mininet
  cd mininet
  sudo make install-mnexec
  $PIP3 .
  popd && sudo rm -rf $TMPDIR

echo "* Done"
