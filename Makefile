
# vim: noexpandtab tabstop=8 shiftwidth=8

LARGE = "192.168.40.93"
SMALL = "192.168.40.42"
#SMALL = "192.168.40.93"


all:
	@echo "make sdist | install | uninstall | bdist"

.phony: all install uninstall sdist bdist

bdist:
	python3 setup.py $@
sdist:
	python3 setup.py $@
install:
	python3 setup.py $@

uninstall:
	pip3 uninstall qllabels
