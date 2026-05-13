SHELL := bash
REPOSITORY := https://repository.genesis-core.tech
ifeq ($(SSH_KEY),)
	SSH_KEY = ~/.ssh/id_rsa.pub
endif

all: help

help:
	@echo "build            - build element"
	@echo "install          - install element"

build:
	genesis build -i $(SSH_KEY) -f --manifest-var repository=$(REPOSITORY)

install:
	genesis elements install output/manifests/dbaas.yaml
