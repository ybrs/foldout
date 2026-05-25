.PHONY: publish-package bump-version

# Bump type: patch (default), minor, major. Or pass an explicit VERSION=x.y.z.
BUMP ?= patch

publish-package:
	@./scripts/publish_package.sh $(BUMP) $(VERSION)

bump-version:
	@./scripts/bump_version.py $(BUMP) $(VERSION)
