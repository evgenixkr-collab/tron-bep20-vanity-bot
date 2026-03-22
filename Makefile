# Makefile для сборки BEP-20 vanity генератора
#
# Требования:
#   - GCC
#   - OpenSSL dev headers (pkg-config openssl)
#   - vanitygen-plusplus/sha3.c (клонировать отдельно)
#
# Использование:
#   make ethvanitygen   - собрать BEP-20 генератор
#   make clean          - удалить собранные файлы

CC = gcc
CFLAGS = -O3 -Wall -Wno-deprecated
SHA3_SRC = vanitygen-plusplus/sha3.c
OPENSSL_CFLAGS = $(shell pkg-config --cflags openssl 2>/dev/null || echo "-I/usr/include/openssl")
OPENSSL_LIBS = $(shell pkg-config --libs openssl 2>/dev/null || echo "-lcrypto")

ethvanitygen: ethvanitygen.c $(SHA3_SRC)
	$(CC) $(CFLAGS) $(OPENSSL_CFLAGS) -I vanitygen-plusplus/ \
		-o ethvanitygen ethvanitygen.c $(SHA3_SRC) \
		$(OPENSSL_LIBS) -lpthread -lm

clean:
	rm -f ethvanitygen

.PHONY: clean
