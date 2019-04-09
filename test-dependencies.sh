#!/bin/bash

echo "* Installing testing (make {test,codecheck}) dependencies"
  pip3 -q install flake8 pylint
  TMPDIR=$(mktemp -d) && pushd $TMPDIR
  git clone https://github.com/mininet/mininet
  pip3 -q install ./mininet
  sudo pip3 -q install ./mininet
  cd mininet && sudo make install-mnexec
  popd && sudo rm -rf $TMPDIR
  sudo apt install openvswitch-switch
  sudo service openvswitch-switch start

echo "* Done"
