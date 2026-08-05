# Работа с ботом на сервере Amazon

## Подключение к серверу

Откройте Terminal на Mac и выполните:

```bash
ssh -i ~/sshama/amazon.pem ubuntu@3.75.201.48
```

Если SSH впервые спросит, доверять ли серверу, введите `yes`.

После подключения приглашение будет выглядеть примерно так:

```text
ubuntu@ip-172-26-8-170:~$
```

Если Amazon изменит публичный IP-адрес сервера, замените `3.75.201.48` в
команде на новый адрес из панели Lightsail. Чтобы этого не происходило,
прикрепите к инстансу Static IP.

## Переход в проект

```bash
cd ~/futures_bot
git branch --show-current
git status
```

Рабочая ветка проекта:

```text
refactor/bitunix-safety
```

## Управление ботом

Запустить бот и включить автоматический запуск после перезагрузки:

```bash
sudo systemctl enable --now futures-bot
```

Остановить:

```bash
sudo systemctl stop futures-bot
```

Запустить ранее остановленный бот:

```bash
sudo systemctl start futures-bot
```

Перезапустить после изменения настроек или кода:

```bash
sudo systemctl restart futures-bot
```

Проверить состояние:

```bash
systemctl status futures-bot --no-pager
```

Отключить бот и его автоматический запуск:

```bash
sudo systemctl disable --now futures-bot
```

## Просмотр логов

Последние 100 строк:

```bash
journalctl -u futures-bot -n 100 --no-pager
```

Логи в реальном времени:

```bash
journalctl -u futures-bot -f
```

Для выхода из просмотра нажмите `Ctrl+C`. Работа самого бота при этом не
останавливается.

## Безопасное обновление кода

Перед обновлением остановите бот:

```bash
sudo systemctl stop futures-bot
cd ~/futures_bot
git pull origin refactor/bitunix-safety
```

Обновите зависимости и выполните тесты:

```bash
venv/bin/python -m pip install -e ".[test]"
venv/bin/python -m pytest -q
```

Запускайте бот только после успешного прохождения тестов:

```bash
sudo systemctl start futures-bot
systemctl status futures-bot --no-pager
```

## Настройки и секреты

Настройки сервера хранятся только в файле `~/futures_bot/.env`. Не добавляйте
его в Git и никому не отправляйте его содержимое.

Открыть настройки вручную на сервере:

```bash
cd ~/futures_bot
nano .env
```

Сохранение в `nano`: `Ctrl+O`, Enter, затем `Ctrl+X`.

Ограничить доступ к файлу:

```bash
chmod 600 ~/futures_bot/.env
```

После изменения настроек перезапустите службу:

```bash
sudo systemctl restart futures-bot
```

Для первой проверки используйте безопасный режим:

```env
TRADING_MODE=dry-run
ENABLE_PRIVATE_WEBSOCKET=false
```

Перед включением `live` отдельно проверьте настройки риска, ключи API,
доступ к Telegram и результаты работы в `dry-run`.

## Проверка сервера

Использование диска:

```bash
df -h
```

Оперативная память и swap:

```bash
free -h
swapon --show
```

Firewall:

```bash
sudo ufw status verbose
```

Для этого бота входящие HTTP-порты не нужны. Разрешённого SSH-порта `22`
достаточно: бот сам устанавливает исходящие соединения с Telegram и Bitunix.

## Выход с сервера

```bash
exit
```

Выход из SSH не останавливает бот, если он запущен через `systemd`.


посмотреть журнал
journalctl -u futures-bot -f

выход с сервера
exit
