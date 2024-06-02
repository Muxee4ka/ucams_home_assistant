[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

<img alt="tns_energo 1" src="images/ucams.webp" width="500"/>

Этот репозиторий содержит настраиваемый компонент для Home Assistant для отображения данных из сервиса THC-Энерго.

# Установка

**Способ 1.** Через [HACS](https://hacs.xyz/) &rarr; Интеграции &rarr; Добавить пользовательский
репозиторий &rarr; https://github.com/Muxee4ka/ucams_home_assistant &rarr; **Ucams** &rarr; Установить

**Способ 2.** Вручную скопируйте папку `tns_energo`
из [latest release](https://github.com/Muxee4ka/ucams_home_assistant/releases/latest) в
директорию `/config/custom_components`.

После установки необходимо перегрузить Home Assistant

# Настройка

[Настройки](https://my.home-assistant.io/redirect/config) &rarr; Устройства и службы
&rarr; [Интеграции](https://my.home-assistant.io/redirect/integrations)
&rarr; [Добавить интеграцию](https://my.home-assistant.io/redirect/config_flow_start?domain=ucams) &rarr; Поиск &rarr; **Ucams**

или нажмите:

[![Добавить интеграцию](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start?domain=ucams)