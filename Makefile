# Default to the Traefik overlay (same as:
# docker compose -f compose.yml -f compose.traefik.yml up -d)
#
# Local stack without Traefik:
#   make up STACK=local
#   make down STACK=local

STACK ?= traefik

ifeq ($(STACK),local)
  COMPOSE_FILES := -f compose.yml
else
  COMPOSE_FILES := -f compose.yml -f compose.traefik.yml
endif

COMPOSE := docker compose $(COMPOSE_FILES)

.DEFAULT_GOAL := help

.PHONY: help up start down stop logs ps status build restart config pull

help:
	@echo "Stack targets (STACK=$(STACK))"
	@echo
	@echo "  make up        Start the stack in the background"
	@echo "  make down      Stop the stack and remove containers"
	@echo "  make logs      Follow container logs (SERVICE=name optional)"
	@echo "  make ps        Show container status"
	@echo "  make build     Rebuild images and start the stack"
	@echo "  make restart   Restart running services"
	@echo "  make config    Render the merged compose config"
	@echo "  make pull      Pull images used by the stack"
	@echo
	@echo "Use STACK=local for compose.yml only, without Traefik."

up start:
	$(COMPOSE) up -d

down stop:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f $(SERVICE)

ps status:
	$(COMPOSE) ps

build:
	$(COMPOSE) up -d --build

restart:
	$(COMPOSE) restart $(SERVICE)

config:
	$(COMPOSE) config

pull:
	$(COMPOSE) pull
