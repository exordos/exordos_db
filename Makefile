SHELL := bash
ifeq ($(SSH_KEY),)
	SSH_KEY = ~/.ssh/id_rsa.pub
endif

all: help

help:
	@echo "build            - build element"
	@echo "install          - install element"

build:
	exordos build -i $(SSH_KEY) -f

install:
	exordos e e install output/manifests/dbaas.yaml
