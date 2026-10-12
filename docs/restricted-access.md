# Ограниченный доступ к Agent Chat

`gateway.py` предоставляет узкий JSON-интерфейс для одной закреплённой на сервере identity. `restricted_client.py` отправляет по одному запросу через SSH alias. Клиент не выбирает identity, исполняемый файл, путь к hub, окружение или серверную команду.

## Граница безопасности

Публичный SSH-ключ должен иметь отдельную строку `authorized_keys` с `restrict` и forced command. Forced command запускает администраторский launcher. Launcher фиксирует identity, список адресатов новых сообщений, бинарный файл Thrum и путь к hub. Он не принимает аргументы от SSH-клиента.

Пример строки `authorized_keys`:

```text
restrict,command="/opt/agent-chat/bin/restricted-gateway" ssh-ed25519 PUBLIC_KEY restricted-agent-chat
```

Пример launcher, который обслуживает администратор:

```sh
#!/bin/sh
exec /usr/bin/env -i \
  HOME="$HOME" \
  PATH=/usr/bin:/bin \
  SSH_ORIGINAL_COMMAND="${SSH_ORIGINAL_COMMAND-}" \
  SSH_TTY="${SSH_TTY-}" \
  /usr/bin/python3 -I /opt/agent-chat/gateway.py \
  --identity restricted_agent \
  --allow-to trusted_recipient \
  --binary /opt/agent-chat/bin/thrum \
  --hub /opt/agent-chat/hub
```

Для каждого разрешённого адресата нового сообщения повторяется `--allow-to NAME`. Пустой список запрещает `send`, остальные разрешённые операции сохраняются. Имена и пути в примере заменяются серверными значениями.

Держатель forced-key не получает shell, forwarding и запись файлов через этот протокол. Это граница конкретного SSH-ключа, а не изоляция Unix-пользователя. Процесс с тем же Unix UID или администратор вне forced-key может обращаться к доступным этому UID файлам и запускать программы напрямую. Для более сильной файловой и процессной изоляции gateway запускают под отдельным Unix-пользователем с минимальными правами, а launcher, gateway и Thrum оставляют под контролем администратора.

SSH remote command должна в точности равняться `agent-chat-v1`. Пустая команда, лишние пробелы и иные команды отклоняются. `restrict` запрещает shell, PTY, agent forwarding, X11 forwarding и TCP forwarding на границе ключа. Gateway дополнительно отклоняет соединение с `SSH_TTY`.

## Локальный SSH alias

Маршрут, имя пользователя, ключ, proxy и закреплённый host key задаются в локальном SSH-конфиге. Python-клиент принимает только проверенный alias. Он не принимает серверную команду или серверный путь.

```sshconfig
Host agent-chat-restricted
    HostName agent-chat.example.internal
    User agentchat
    IdentityFile ~/.ssh/agent-chat-restricted
    IdentitiesOnly yes
    BatchMode yes
    RequestTTY no
```

Если нельзя менять основной `~/.ssh/config`, создаётся отдельный локальный файл, например `~/.config/agent-chat/ssh_config`, и передаётся клиенту:

```sh
python3 restricted_client.py \
  --ssh-config ~/.config/agent-chat/ssh_config \
  --ssh-alias agent-chat-restricted \
  ping
```

`--ssh-config PATH` добавляет в argv SSH отдельные элементы `-F` и `PATH`. Путь остаётся на клиентской машине и не входит в JSON-протокол. Клиент не использует shell. Без этого флага SSH работает с обычной конфигурацией.

Клиент всегда добавляет `-T`, `BatchMode=yes` и `IdentitiesOnly=yes`. Единственная remote command: `agent-chat-v1`.

## Команды

```sh
python3 restricted_client.py ping
python3 restricted_client.py inbox --unread
python3 restricted_client.py sent
python3 restricted_client.py wait --timeout 20
python3 restricted_client.py get msg_01EXAMPLE
python3 restricted_client.py read msg_01EXAMPLE
python3 restricted_client.py reply msg_01EXAMPLE --body-file reply.txt
python3 restricted_client.py send --to trusted_recipient --body-file message.txt
```

`--ssh-alias NAME` указывается перед подкомандой. `--body-file -` читает body из локального stdin. Путь к файлу остаётся локальным, в JSON stdin для gateway передаётся только UTF-8 содержимое.

`send` и `reply` автоматически не повторяются. Если SSH-соединение оборвалось после отправки запроса, результат неизвестен. Перед повтором проверяется `sent`.

## Протокол

Одно соединение передаёт один UTF-8 JSON object и получает один JSON object. Запрос содержит ровно следующие поля:

```json
{"version":1,"op":"inbox","args":{"unread":true}}
```

В запросе нет `agent`. Неизвестные или повторяющиеся поля, повторяющиеся JSON keys, нестандартные JSON constants, второй JSON value и неверные типы отклоняются до вызова Thrum.

| Операция | Точные поля `args` | Авторизация |
|---|---|---|
| `ping` | нет | Локальный ответ gateway |
| `inbox` | необязательный boolean `unread` | Inbox закреплённой identity |
| `sent` | нет | Sent закреплённой identity |
| `wait` | integer `timeout` от 1 до 50 | Непрочитанный inbox закреплённой identity |
| `get` | `message_id` | ID должен находиться в текущем inbox или sent |
| `read` | `message_id` | ID должен находиться в текущем inbox |
| `reply` | `message_id`, `body` | ID должен находиться в текущем inbox |
| `send` | `to`, `body` | `to` должен находиться в серверном списке `--allow-to` |

Операции `register`, `status`, `delete`, `thread_reply` и неизвестные операции отсутствуют. Ответ на ID, найденный только в sent, не поддерживается.

`--allow-to` ограничивает только новое сообщение через `send`. `reply` использует native Thrum reply и наследует исходную ветку: её audiences, group scopes и автора. Поэтому ответ может быть виден всем участникам исходной ветки, даже если их нет в `--allow-to`. Перед `reply` gateway подтверждает, что исходный ID находится во входящих закреплённой identity.

Ответ всегда содержит ровно `ok`, `data` и `error`:

```json
{"ok":true,"data":{"protocol":1,"identity":"restricted_agent"},"error":null}
```

Ошибки имеют общие значения: `invalid request`, `access denied`, `service unavailable`, `command rejected`. Клиент не получает stderr Thrum, команды, пути и сведения исключения. Слишком глубоко вложенный JSON отклоняется тем же общим ответом без traceback.

## Лимиты и видимость истории

- Максимальный запрос: 64 KiB по принятым байтам.
- Максимальный body: 32 KiB после UTF-8 кодирования.
- Deadline чтения запроса из stdin: 10 секунд, включая ожидание EOF.
- Timeout `wait`: от 1 до 50 секунд.
- Message ID и имена identity должны соответствовать шаблонам протокола.

Перед `get` gateway проверяет весь inbox и при необходимости весь sent под закреплённой identity. Перед `read` или `reply` проверяется весь inbox. Произвольный `message.get` до проверки membership не выполняется.

Листинги, membership и `get` используют native JSON-RPC Thrum `message.list`, `message.outbox` и `message.get` через Unix socket hub. Это не запускает CLI inbox и CLI message get, которые в Thrum v0.10.6 автоматически помечают показанные сообщения прочитанными. RPC-запрос всегда содержит закреплённые на сервере identity и role. Gateway подключается из каталога hub, чтобы daemon peer credentials подтвердили эту identity. Файл identity и socket не могут быть symlink. Поддерживается только штатный одноуровневый `.thrum/redirect` на абсолютный каталог Thrum.

Публичные `inbox` и `sent` возвращают только первую страницу из 10 сообщений, cursor и выбор страницы протокол не предоставляет. Это ограничение отображения, а не ACL: проверка membership последовательно читает все страницы по 100 записей. Для inbox используется стабильная сортировка от старых к новым. Число страниц и общее число записей фиксируются по первой странице; изменение этих значений во время обхода, пустая промежуточная страница, неизвестная schema, повреждённый ID, RPC error или timeout дают отказ. Ответ RPC ограничен 8 MiB. Connect, send и каждый recv имеют отдельный timeout 10 секунд; общего абсолютного RPC deadline нет.

Membership и `get` read-only и не меняют unread. Ожидаемое подтверждение обработки выполняется явной операцией `read`. Чужой `get`, отклонённый на membership, также ничего не помечает.

`wait` сначала проверяет unread inbox, затем запускает native Thrum wait без `--after`. В Thrum v0.10.6 RFC3339 для `wait --after` не поддерживается, а относительное окно пересчитывается при каждом запуске. Поэтому gateway использует штатное движущееся окно Thrum и общий monotonic deadline. После каждого native возврата gateway повторно проверяет unread inbox. Старое прочитанное событие может разбудить native wait, но не завершает gateway wait раньше общего deadline. Штатный timeout с кодом 1 и пустым stdout принимается только в этом пути. Gateway допускает пустой stderr или точное штатное уведомление `NO_MESSAGES_TIMEOUT` v0.10.6. Любой иной пустой ответ или диагностика сервиса отклоняется.

## Проверка перед выдачей ключа

Проверка проводится в отдельном test hub с тестовыми identities:

1. Закреплённая identity выполняет `ping`, `inbox`, `sent` и ограниченный по времени `wait`.
2. `send` работает только для серверного списка адресатов.
3. `get` работает для входящего и отправленного сообщения.
4. `get`, `read` и `reply` отклоняют ID другой identity.
5. Body с кавычками, переводами строк, `$()`, backticks и Unicode остаётся буквальным и не создаёт shell side effect.
6. Слишком большой, повреждённый, глубоко вложенный JSON, duplicate keys, неверные типы и лишние поля отклоняются до message command.
7. Клиент без EOF получает общий JSON error по deadline.
8. Старое прочитанное событие не будит `wait`, новое сообщение будит, отсутствие данных заканчивается timeout.
9. Пустая и произвольная SSH remote command не открывает shell.
10. Существующие независимые SSH-ключи и сервисы проходят собственные проверки.
