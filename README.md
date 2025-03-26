# ProxyBrowser API — Инструкция по установке и запуску

> ℹ️ Внимание: угловые скобки (`< >`) в примерах обозначают **пользовательские данные**, которые нужно заменить на свои значения.
> Например, `<GIT>` → `YourProfilGIT`, `<VERSION>` → `1.0.2`

## Описание проекта
ProxyBrowser API — это FastAPI-сервис, обслуживающий клиентское приложение (браузер), выдающий файлы, принимающий логи и обновляющий состояние базы.

---

## Структура проекта

```
app_phone/
│   
├── database/
│   ├── db.py                      # Класс работы с базой
│   └── models.py                  # SQLAlchemy ORM модели
│ 
├── docs/
│   ├── images                     # Папка с изображениями для инструкции
│   │   ├── call.png
│   │   ├── call2.png
│   │   ├── sms.png
│   │   └── sms2.png
│   │ 
│   └── novofon_setup_guide.md     # Настройка уведомлений Novofon
│  
├── version/                       # Папка для хранения версий
│  
├── .gitignore                     # Исключения для git
├── config.example.py              # Пример конфигурации (копируется в config.py)
├── main.py                        # Точка входа FastAPI
│   
├── requirements.txt               # Список зависимостей проекта
│   
└── README.md                      # Этот файл
```

---

## Установка проекта на сервер

### 1. Склонируй репозиторий
```bash
cd /home/
git clone https://github.com/<GIT>/api_phone
cd api_phone
```

### 2. Создай виртуальное окружение и установи зависимости
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Скопируй config.example.py

Создайте файл:

```bash
cp config.example.py config.py
```

И отредактируйте:

```python
DB_USER = "your_user"
DB_PASS = "your_password"
DB_HOST = "your_host"
DB_NAME = "your_db"
```

---

## Запуск апи
```bash
uvicorn main:app --host 0.0.0.0 --port 2613 --reload
```

### Автозапуск (сервис systemd)
Создай файл `/etc/systemd/system/fastapi.service`:

```ini
[Unit]
Description=FastAPI Service
After=network.target

[Service]
User=root
WorkingDirectory=/home/api_phone
ExecStart=/home/api_phone/venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 2613
Restart=always

[Install]
WantedBy=multi-user.target
```

Затем:
```bash
sudo systemctl daemon-reexec
sudo systemctl enable fastapi
sudo systemctl start fastapi
```

---

## Обновление проекта на сервере

### Чтобы обновить код:
```bash
cd /home/api_phone
sudo systemctl stop fastapi
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl start fastapi
```

---

## Как добавить новую версию браузера

1. Собери заранее `.zip` архив: `browser-<VERSION>.zip` содержащий новую версию `ProxyBrowser`
2. Помести его в каталог `FILE_PATH` на сервере
3. Обнови строку таблицы `version` в базе:
```
version = <VERSION>
url = 'http://<IP>:2613/download_app'
```
4. При запуске клиент сам получит обновление.
5. В проекте `DesktopBrowser` обновите файл `config.py`:
```python
LOG_SERVER_URL = "http://<IP>:2613/log"
```

---

## 📎 Дополнительно

📘 Ознакомьтесь с [инструкцией по настройке Novofon API и Telegram уведомлений](docs/novofon_setup_guide.md)

---

## ✅ Готово!

Теперь сервер Ubuntu с API готов для использования с DesktopBrowser
