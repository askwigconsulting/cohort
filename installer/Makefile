# Cohort install/uninstall targets — thin delegators to the CLI.
#
# Usage:
#   make -C installer install IDE=claude,cursor
#   make -C installer install IDE=all COPY=1
#   make -C installer uninstall
#
# COHORT overrides the CLI binary (defaults to `cohort` on PATH).

COHORT ?= cohort
IDE ?=
COPY ?=
FORCE ?=

ifneq ($(strip $(IDE)),)
  IDE_ARG := --ide $(IDE)
endif
ifneq ($(strip $(COPY)),)
  COPY_ARG := --copy
endif
ifneq ($(strip $(FORCE)),)
  FORCE_ARG := --force
endif

.PHONY: install uninstall

install:
	$(COHORT) install $(IDE_ARG) $(COPY_ARG) $(FORCE_ARG)

uninstall:
	$(COHORT) uninstall
