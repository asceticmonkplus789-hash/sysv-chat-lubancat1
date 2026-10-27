CC = cc
CFLAGS = -std=c11 -O2 -Wall -Wextra -Wpedantic -Werror
THREAD_FLAGS = -pthread
TARGET ?= chat

.PHONY: all test
all: $(TARGET)

$(TARGET): chat.c
	$(CC) $(CPPFLAGS) $(CFLAGS) $(THREAD_FLAGS) chat.c $(LDFLAGS) -o $@

test: $(TARGET)
	python3 tests/test_chat.py ./$(TARGET)
