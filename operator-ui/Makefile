.PHONY: test preview

WORKTREE_ROOT := $(abspath ..)
DIRENV_ROOT ?= $(WORKTREE_ROOT)

test:
	direnv exec "$(DIRENV_ROOT)" env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s "$(WORKTREE_ROOT)/operator-ui/tests" -v

preview:
	direnv exec "$(DIRENV_ROOT)" python3 -m http.server 4173 --directory "$(WORKTREE_ROOT)/operator-ui"
