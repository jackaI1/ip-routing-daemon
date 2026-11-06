CXX ?= c++
CXXFLAGS ?= -std=c++20 -O2 -g -Wall -Wextra -Wpedantic -Werror
CPPFLAGS += -Isrc
ifeq ($(SANITIZE),1)
CXXFLAGS += -O1 -fsanitize=address,undefined -fno-omit-frame-pointer
LDFLAGS += -fsanitize=address,undefined
endif
ifeq ($(shell uname -s),Linux)
ALL = build/routerd build/core-tests
ifeq ($(SANITIZE),1)
CXXFLAGS += -fno-pie
LDFLAGS += -no-pie
endif
else
ALL = build/core-tests
endif
.PHONY: all test clean
all: $(ALL)
build:
	mkdir -p build
build/routerd: src/main.cpp src/router.cpp src/router.hpp | build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) src/main.cpp src/router.cpp $(LDFLAGS) -o $@
build/core-tests: tests/core.cpp src/router.cpp src/router.hpp | build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) tests/core.cpp src/router.cpp $(LDFLAGS) -o $@
test: all
	python3 -m pytest -q
clean:
	rm -rf build
