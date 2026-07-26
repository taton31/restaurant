import time
import random

class YandexEdaHeaderEmulator:
    @staticmethod
    def _base36_encode(number: int) -> str:
        """Переводит число в 36-ричную систему счисления (0-9, a-z)."""
        num_str = "0123456789abcdefghijklmnopqrstuvwxyz"
        if number == 0:
            return "0"
        base36 = []
        while number:
            number, i = divmod(number, 36)
            base36.append(num_str[i])
        return "".join(reversed(base36))

    @classmethod
    def _generate_block(cls) -> str:
        """Генерирует случайный блок (аналог Math.random().toString(36))."""
        # Генерируем случайное число с плавающей точкой, как Math.random()
        rand_num = random.random()
        # Превращаем его в целое число для кодирования (отбрасывая "0.")
        int_val = int(str(rand_num)[2:14])
        return cls._base36_encode(int_val)[:10]

    @classmethod
    def generate_id(cls) -> str:
        """Генерирует строку формата Яндекса (ms1tzgsj-n8ptvibaiyi-...)."""
        # Текущее время в миллисекундах
        timestamp_ms = int(time.time() * 1000)
        
        # Первый блок на основе времени
        part1 = cls._base36_encode(timestamp_ms)
        
        # Остальные 3 блока — случайные
        part2 = cls._generate_block()
        part3 = cls._generate_block()
        part4 = cls._generate_block()
        
        return f"{part1}-{part2}-{part3}-{part4}"

# Пример использования:
emulator = YandexEdaHeaderEmulator()

# headers = {
#     'x-device-id': emulator.generate_id(),      # Генерируем один раз для "устройства"
#     'x-client-session': emulator.generate_id(),  # Генерируем заново для каждой новой сессии/запуска
#     'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36...',
# }

# print(headers)
