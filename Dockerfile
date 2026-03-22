FROM python:3.11-slim

# Системные зависимости для компиляции vanitygen++ и ethvanitygen
RUN apt-get update && apt-get install -y \
    gcc \
    make \
    git \
    libssl-dev \
    libpcre3-dev \
    libpthread-stubs0-dev \
    stdbuf \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1. Клонируем и компилируем vanitygen++ (для TRC-20)
RUN git clone --depth=1 https://github.com/10gic/vanitygen-plusplus vanitygen-plusplus \
    && cd vanitygen-plusplus \
    && make vanitygen++ \
    && strip vanitygen++

# 2. Копируем исходник BEP-20 генератора
COPY ethvanitygen.c .

# 3. Компилируем ethvanitygen (для BEP-20)
RUN gcc -O3 -Wall -Wno-deprecated \
    -I vanitygen-plusplus/ \
    -o ethvanitygen \
    ethvanitygen.c vanitygen-plusplus/sha3.c \
    -lcrypto -lpthread -lm \
    && strip ethvanitygen

# 4. Устанавливаем Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Копируем код бота
COPY main.py .

# Запускаем бота
CMD ["python", "main.py"]
