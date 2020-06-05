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

srcs = faucetagent.py agenttest.py

# Set a default GOPATH if needed for gnmi_* tools used in test
GOPATH ?= $(HOME)/go

all: $(gnmistubs) $(extstubs)

$(gnmiproto):
# We fetch and edit the .proto to avoid a deep directory hierarchy
	wget $(urlbase)/gnmi/$@
	sed -i $@ -e 's|github.com/openconfig/gnmi/proto/gnmi_ext/||'

$(extproto):
	wget $(urlbase)/gnmi_ext/$@

$(gnmistubs): $(gnmiproto) $(extproto)
	$(protoc) $<

$(extstubs): $(extproto)
	$(protoc) $<

test: all
	@echo "* Using GOPATH=$(GOPATH)"
	GOPATH=$(GOPATH) PATH=$(GOPATH)/bin:$(PATH) ./agenttest.py -v

codecheck: all
	flake8 $(srcs)
	pylint $(srcs)
	pytype $(srcs)

yapf:
	yapf3 -i $(srcs)

clean:
	rm -rf *.proto *_pb2*.py *.pyc *~ \#*\# testcerts *.log __pycache__ *.yaml
