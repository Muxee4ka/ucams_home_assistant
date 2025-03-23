[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

![ucams 1](images/icons/logo.png)

Этот репозиторий содержит настраиваемый компонент для Home Assistant для отображения Видеонаблюдения от Уфанет.

# Установка

**Способ 1.** Через [HACS](https://hacs.xyz/) &rarr; Интеграции &rarr; Добавить пользовательский
репозиторий &rarr; https://github.com/Muxee4ka/ucams_home_assistant &rarr; **Ucams** &rarr; Установить

**Способ 2.** Вручную скопируйте папку `ucams`
из [latest release](https://github.com/Muxee4ka/ucams_home_assistant/releases/latest) в
директорию `/config/custom_components`.

После установки необходимо перегрузить Home Assistant

# Настройка

[Настройки](https://my.home-assistant.io/redirect/config) &rarr; Устройства и службы
&rarr; [Интеграции](https://my.home-assistant.io/redirect/integrations)
&rarr; [Добавить интеграцию](https://my.home-assistant.io/redirect/config_flow_start?domain=ucams) &rarr; Поиск &rarr; **Ucams**

или нажмите:

[![Добавить интеграцию](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start?domain=ucams)

# Автоматизации

```yaml
alias: Сделать снимок камеры 1
description: ""
trigger:
  - platform: time
    at: "01:00:00"
condition:
  - condition: template
    value_template: "{{ now().day == 24 }}"
action:
  target:
  entity_id: camera.ucams_kamera_1
  data:
    filename: www/kamera_1.png
  action: ucams.snapshot
```

```yaml
alias: "Архив за последний час в Telegram"
description: "Запрашивает архив за последний час и отправляет ссылку в Telegram."
trigger:
  - platform: time_pattern
    minutes: "0"  # срабатывает каждый час, когда минуты равны 0
condition: []
action:
  # 1. Запрашиваем архив за последний час
  - service: ucams.get_archive
    data:
      entity_id: camera.ucams_kamera_1
      start_time: "{{ (now().timestamp() | int) - 3600 }}"
      duration: 3600
  # 2. Небольшая задержка для обновления состояния ArchiveLinkSensor
  - delay:
      seconds: 10
  # 3. Отправляем сообщение в Telegram с ссылкой на архив.
  # Предполагается, что ArchiveLinkSensor для данной камеры имеет entity_id sensor.archive_link_kamera16_3podezd_kryltso
  - service: telegram_bot.send_message
    data:
      target:
        - "<your_telegram_chat_id>"  # замените на ваш chat_id
      message: "Архив за последний час: [Открыть архив]({{ state_attr('sensor.archive_link_kamera16_3podezd_kryltso', 'archive_url') }})"
mode: single
```

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=Muxee4ka/ucams_home_assistant&type=Timeline)](https://star-history.com/#Muxee4ka/ucams_home_assistant&Timeline)
