# Base URL for GNMI .proto files
urlbase = https://raw.githubusercontent.com/openconfig/gnmi/master/proto

# GNMI .proto files
gnmiproto = gnmi.proto
extproto = gnmi_ext.proto

# Python interfaces for GRPC, automatically generated from .proto files
gnmistubs = gnmi_pb2.py gnmi_pb2_grpc.py
extstubs = gnmi_ext_pb2.py gnmi_ext_pb2_grpc.py

# GRPC/protobuf compiler command
protoc = python3 -m grpc_tools.protoc --python_out=. --grpc_python_out=. -I.

# Go path - required for gnmi_{set,get}
GOPATH = $(HOME)/go

all: $(gnmistubs) $(extstubs)

$(gnmiproto):
# We fetch and edit the .proto to avoid a deep directory hierarchy
	wget $(urlbase)/gnmi/$@
	sed -i $@ -e 's|github.com/openconfig/gnmi/proto/gnmi_ext/||'

$(extproto):
	wget $(urlbase)/gnmi_ext/$@

$(gnmistubs): gnmi.proto gnmi_ext.proto
	$(protoc) $<

$(extstubs): gnmi_ext.proto
	$(protoc) $<


codecheck: $(gnmistubs) $(extstubs)
	flake8 faucetagent.py agenttest.py
	python3 -m pylint faucetagent.py agenttest.py

yapf:
	yapf3 -i *py

clean:
	rm -rf *proto *pb2*py *pyc *~ \#*\# testcerts *log __pycache__ *yaml

test: all
	GOPATH=$(GOPATH) PATH=$(GOPATH)/bin:$(PATH) ./agenttest.py
	grep faucet_config_applied faucetagent.log || true
