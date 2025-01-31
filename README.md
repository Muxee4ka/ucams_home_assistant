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

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=Muxee4ka/ucams_home_assistant&type=Timeline)](https://star-history.com/#Muxee4ka/ucams_home_assistant&Timeline)
