# Работа с ботом на сервере Amazon

## Подключение к серверу

Откройте Terminal на Mac и выполните:

```bash
ssh -i ~/sshama/amazon.pem ubuntu@<SERVER_IP>
```

Если SSH впервые спросит, доверять ли серверу, введите `yes`.

После подключения приглашение будет выглядеть примерно так:

```text
ubuntu@ip-172-26-8-170:~$
```

Замените `<SERVER_IP>` на адрес из панели Lightsail. Чтобы адрес не менялся,
прикрепите к инстансу Static IP. Конкретный адрес сервера не храните в Git.

## Подключение с другого компьютера

Для подключения с другого Mac потребуется приватный SSH-ключ `amazon.pem`.
Файл `.env` переносить на другой компьютер не нужно: он уже находится на
сервере.

Безопасно перенесите `amazon.pem` через AirDrop, зашифрованную флешку или
другой доверенный канал. Не отправляйте приватный ключ в Telegram, обычной
электронной почте, публичном облаке или Git.

На другом Mac создайте папку для SSH-ключей и переместите туда ключ:

```bash
mkdir -p ~/.ssh
mv ~/Downloads/amazon.pem ~/.ssh/amazon.pem
chmod 700 ~/.ssh
chmod 600 ~/.ssh/amazon.pem
```

Подключитесь к серверу:

```bash
ssh -i ~/.ssh/amazon.pem ubuntu@<SERVER_IP>
```

Если SSH сообщает `UNPROTECTED PRIVATE KEY FILE`, ещё раз установите права:

```bash
chmod 600 ~/.ssh/amazon.pem
```

После подключения управление службой выполняется теми же командами:

```bash
systemctl status futures-bot --no-pager
sudo systemctl start futures-bot
sudo systemctl stop futures-bot
sudo systemctl restart futures-bot
journalctl -u futures-bot -f
```

Для постоянной работы безопаснее позднее создать отдельный SSH-ключ на
новом компьютере и добавить только его публичную часть на сервер. Тогда при
потере одного компьютера соответствующий ключ можно будет удалить, не меняя
ключи на остальных устройствах.

### Создание отдельного ключа на втором Mac

На втором компьютере откройте Terminal и создайте отдельную пару ключей:

```bash
ssh-keygen -t ed25519 -C "second-mac-amazon"
```

Нажмите Enter, чтобы сохранить ключ по стандартному пути
`~/.ssh/id_ed25519`. Желательно установить парольную фразу. Будут созданы два
файла:

- `~/.ssh/id_ed25519` — приватный ключ, его нельзя никому передавать;
- `~/.ssh/id_ed25519.pub` — публичный ключ, его нужно добавить на сервер.

Покажите и скопируйте публичный ключ:

```bash
cat ~/.ssh/id_ed25519.pub
```

Скопируйте всю строку, которая начинается с `ssh-ed25519`.

На первом компьютере подключитесь к серверу существующим ключом:

```bash
ssh -i ~/sshama/amazon.pem ubuntu@<SERVER_IP>
```

Откройте список разрешённых публичных ключей:

```bash
nano ~/.ssh/authorized_keys
```

Добавьте публичный ключ второго компьютера отдельной новой строкой. Не
удаляйте уже существующие строки. Сохраните файл: `Ctrl+O`, Enter, затем
`Ctrl+X`.

Проверьте права:

```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys
```

Не закрывая подключение с первого компьютера, на втором Mac проверьте новый
ключ:

```bash
ssh ubuntu@<SERVER_IP>
```

Закрывайте старое подключение только после успешного входа с нового
компьютера. Если ключ был сохранён под нестандартным именем, укажите путь:

```bash
ssh -i ~/.ssh/имя_ключа ubuntu@<SERVER_IP>
```

Для короткой команды подключения создайте на втором Mac файл
`~/.ssh/config`:

```text
Host futures-bot
    HostName <SERVER_IP>
    User ubuntu
    IdentityFile ~/.ssh/id_ed25519
```

Установите безопасные права и подключайтесь по имени:

```bash
chmod 600 ~/.ssh/config
ssh futures-bot
```

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

### Установка системной службы

Amazon использует отдельный серверный шаблон
`deploy/futures-bot-server.service`. Ноутбучный файл
`deploy/futures-bot.service` для сервера не подходит: в нём другой домашний
каталог и используется блокировка сна ноутбука.

Шаблон предполагает:

- пользователя `ubuntu`;
- проект в `/home/ubuntu/futures_bot`;
- Python в `/home/ubuntu/futures_bot/venv/bin/python`.

Если пути на сервере отличаются, сначала исправьте копию unit-файла. Затем
установите службу:

```bash
cd ~/futures_bot
sudo cp deploy/futures-bot-server.service \
  /etc/systemd/system/futures-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now futures-bot
```

Проверьте, какой файл действительно загружен системой:

```bash
systemctl cat futures-bot
systemctl status futures-bot --no-pager
```

После изменения unit-файла всегда выполняйте `sudo systemctl daemon-reload`.

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
git pull --ff-only origin refactor/bitunix-safety
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
AUTO_MOVE_STOP_LOSS_ON_TP1=true
ENABLE_EMULATED_ENTRY_TRIGGERS=true
TRIGGER_LIMIT_OFFSET_TICKS=2
```

Обе автоматические функции явно включены: после подтверждённого исполнения
TP1 бот переносит SL в безубыток, а локальные триггерные планы могут отправить
LIMIT-вход со смещением при достижении цены. В `dry-run` реальные ордера не
создаются.

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
