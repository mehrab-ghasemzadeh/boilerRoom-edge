# Boiler Room Monitoring System Design

## Overview
This document summarizes the hardware architecture described in the source document with one intentional design change: the MQ-2 gas sensors are interfaced to an **ATtiny13** microcontroller, which performs analog-to-digital conversion and forwards digital measurements to the Raspberry Pi. No external ADC is used.

## Main Controller
- Raspberry Pi Zero v1.3
- Linux operating system
- Executes the monitoring and control software

## SPI Bus
- MOSI: GPIO10
- MISO: GPIO9
- SCLK: GPIO11
- CE0: Available for peripherals
- CE1: ST7920 LCD

## Gas Sensor Subsystem
- MQ-2 gas sensors produce analog signals.
- An ATtiny13 samples the analog channels using its internal ADC.
- The ATtiny13 periodically converts sensor values and transfers the digital readings to the Raspberry Pi through a simple serial interface.
- The Raspberry Pi treats the ATtiny13 as a sensor interface processor and does not perform any analog conversion itself.

## Display
ST7920 128x64 graphical LCD connected over SPI.

## Temperature Sensors
DS18B20 sensors share a single 1-Wire bus on GPIO4 with a 4.7kΩ pull-up resistor. Each sensor has a unique 64-bit address.

## Relay Outputs
GPIO17, GPIO18, GPIO27, GPIO22, GPIO23, GPIO24, GPIO25, GPIO12.

## Keypad
GPIO5, GPIO6, GPIO13, GPIO16, GPIO19, GPIO20, GPIO21, GPIO26.

## Software Responsibilities
- Read DS18B20 temperatures.
- Receive gas measurements from the ATtiny13.
- Update LCD.
- Scan keypad.
- Control relays.
- Store telemetry and configuration.
