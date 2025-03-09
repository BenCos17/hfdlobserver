# decoders.py
# copyright 2025 Kuupa Ork <kuupaork+github@hfdl.observer>
# see LICENSE (or https://github.com/hfdl-observer/hfdlobserver888/blob/main/LICENSE) for terms of use.
# TL;DR: BSD 3-clause
#

import asyncio
import asyncio.subprocess
import collections
import collections.abc
import logging
import random

from typing import Any, Callable, Mapping, Optional

import hfdl_observer.data
import hfdl_observer.env as env
import hfdl_observer.process
import hfdl_observer.util as util


logger = logging.getLogger(__name__)


class DumphfdlCommand(hfdl_observer.process.Command):
    pass


class IQDecoderCommand(DumphfdlCommand):
    pass


class BaseDecoder:
    listener: hfdl_observer.data.ListenerConfig
    channel: hfdl_observer.data.ObservingChannel
    config: collections.abc.Mapping
    # task: Optional[asyncio.Task] = None

    def __init__(self, name: str, config: dict, listener: hfdl_observer.data.ListenerConfig) -> None:
        self.name = name
        self.config = config
        self.listener = listener
        super().__init__()

    @property
    def station_id(self) -> Optional[str]:
        return self.config.get('station_id', None)

    async def start(self) -> None:
        raise NotImplementedError()

    async def stop(self) -> None:
        raise NotImplementedError()

    async def listen(self, channel: hfdl_observer.data.ObservingChannel) -> None:
        raise NotImplementedError()

    def __str__(self) -> str:
        return f'{self.__class__.__name__}@{self.name}'


class Dumphfdl(BaseDecoder):
    def listen_args(self) -> list[str]:
        raise NotImplementedError()

    def commandline(self) -> list[str]:
        if not self.channel or not self.channel.frequencies:
            logger.warning(f'{self} requested an empty command line')
            return []
        cmd = [str(env.as_executable_path(self.config['decoder_path']))]
        cmd.extend(self.listen_args())
        # map some common options
        normalizer: Callable[[Any], Any]
        opt_map: list[tuple[str, str, Callable]] = [
            ('system_table', 'system-table', env.as_path),
            ('system_table_save', 'system-table-save', env.as_path),
            ('station_id', 'station-id', lambda x: x),
        ]
        for from_opt, to_opt, normalizer in opt_map:
            value = self.config.get(from_opt, None)
            if value is not None:
                cmd.extend([f'--{to_opt}', str(normalizer(value))])
        try:
            cmd.extend([
                '--statsd', str(self.config['statsd_server']).replace(' ', ':'),
                '--noise-floor-stats-interval', '30',
            ])
        except KeyError:
            pass
        # Add a special output that sends to our local listener. We could do this through pipes, but this may be
        # simpler for multiple receivers, especially remote ones.
        cmd.extend([
            '--output', f'decoded:json:{self.listener.proto}:address={self.listener.address},port={self.listener.port}'
        ])

        # If we have a station ID, send to airframes (unless it's a private station ID starting with '*')
        if self.station_id and not self.station_id.startswith('*'):
            cmd.extend(['--output', 'decoded:json:tcp:address=feed.airframes.io,port=5556',])

        # add any other configured outputs, such as for acarshub
        for out in self.config.get('output', None) or []:
            cmd.extend([
                '--output',
                f'decoded:{out.get("format", "json")}:{out["protocol"]}:address={out["address"]},port={out["port"]}'
            ])

        # console logging, unless told not to
        if not self.config.get('quiet', False):
            cmd.extend(['--output', 'decoded:text:file:path=/dev/stdout',])

        # packet logging, rotated daily (if configured)
        try:
            packetlog = env.as_path(self.config['packetlog'])
        except KeyError:
            pass
        else:
            if packetlog.is_dir():
                packetlog = packetlog / f'{self.name}.log'
            cmd.extend(['--output', f'decoded:json:file:path={packetlog},rotate=daily',])

        # now add all the frequencies we're watching
        cmd.extend(str(f) for f in self.channel.frequencies)
        return cmd

    def valid_return_codes(self) -> list[int]:
        # 0 : OK
        # 1 : ??? seems to be related to pipe or usb problems?
        # -6 : SIGABRT. "The futex facility returned an unexpected error code."
        # -11 is speculative. Some weirdness on odroid
        return [0, 1, -6, -11, -15]


class IQDecoder(Dumphfdl):
    def listen_args(self) -> list[str]:
        return [
            '--iq-file', '-',
            '--sample-rate', str(self.channel.allowed_width_hz),
            '--sample-format', 'CS16',
            '--read-buffer-size', '9600',
            '--centerfreq', str(self.channel.center_khz),
        ]


class IQDecoderProcess(hfdl_observer.process.ProcessHarness, IQDecoder):
    pipe: util.Pipe

    def __init__(self, name: str, config: dict, listener: hfdl_observer.data.ListenerConfig) -> None:
        IQDecoder.__init__(self, name, config, listener)
        hfdl_observer.process.ProcessHarness.__init__(self)
        self.settle_time = config.get('settle_time', 0) + random.randrange(1, 1000) / 1000.0

    def commandline(self) -> list[str]:
        return IQDecoder.commandline(self)

    def execution_arguments(self) -> dict:
        return {
            'stdin': self.pipe.read,
        }

    def on_execute(self, process: asyncio.subprocess.Process, context: Any) -> None:
        self.pipe.close_read()

    async def listen(self, channel: hfdl_observer.data.ObservingChannel) -> None:
        self.channel = channel
        await self.start()

    def create_command(self) -> IQDecoderCommand:
        cmd = self.commandline()
        # self.logger.info(f'CMD: {cmd}')
        command = IQDecoderCommand(
            self.logger,
            cmd,
            self.execution_arguments(),
            on_prepare=self.on_prepare,
            on_running=self.on_execute,
            valid_return_codes=self.valid_return_codes(),
        )
        return command


class DummyDecoder(IQDecoderProcess):
    async def listen(self, channel: hfdl_observer.data.ObservingChannel) -> None:
        self.channel = channel
        logger.debug(f'{self} command {self.commandline()}')
        logger.debug(f'{self} listening on {channel}')
        await asyncio.sleep(0.1)


class DirectDecoder(hfdl_observer.process.ProcessHarness, Dumphfdl):
    def __init__(self, name: str, config: dict, listener: hfdl_observer.data.ListenerConfig) -> None:
        Dumphfdl.__init__(self, name, config, listener)
        hfdl_observer.process.ProcessHarness.__init__(self)
        self.settle_time = config.get('settle_time', 0) + random.randrange(1, 1000) / 1000.0

    def create_command(self) -> DumphfdlCommand:
        command = DumphfdlCommand(
            self.logger,
            self.commandline(),
            self.execution_arguments(),
            on_prepare=self.on_prepare,
            on_running=self.on_execute,
            valid_return_codes=self.valid_return_codes(),
            unrecoverable_errors=['Sample buffer overrun'],
        )
        return command

    def commandline(self) -> list[str]:
        return Dumphfdl.commandline(self)

    def execution_arguments(self) -> dict:
        return {}

    def on_execute(self, process: asyncio.subprocess.Process, context: Any) -> None:
        pass

    async def listen(self, channel: hfdl_observer.data.ObservingChannel) -> None:
        self.channel = channel
        await self.start()

    def observable_channel_widths(self) -> list[int]:
        raise NotImplementedError(str(self.__class__))


class SoapySDRDecoder(DirectDecoder):
    def __init__(self, name: str, config: dict, listener: hfdl_observer.data.ListenerConfig) -> None:
        super().__init__(name, config, listener)
        self.sample_rates = sorted(util.normalize_ranges(config.get('sample-rates', [])))

    def listen_args(self) -> list[str]:
        def fixup(o: Any) -> str:
            if o is True:
                return 'true'
            if o is False:
                return 'false'
            s = str(o)
            if s.lower() in ['true', 'false']:
                return s.lower()
            return s

        def nested_args(incoming: Mapping | str) -> str:
            if isinstance(incoming, collections.abc.Mapping):
                return ','.join(f'{k}={fixup(v)}' for k, v in incoming.items())
            else:
                return incoming
        args = []
        arg_map = [
            ('gain', 'gain', None),
            ('antenna', 'antenna', None),
            ('freq-offset', 'freq-offset', None),
            ('freq-correction', 'freq-correction', None),
            ('soapysdr', 'soapysdr', nested_args),
            ('gain-elements', 'gain-elements', nested_args),
            ('device-settings', 'device-settings', nested_args),
        ]
        for from_opt, to_opt, normalizer in arg_map:
            value = self.config.get(from_opt, None)
            if value is not None:
                opt_value: str
                if normalizer:
                    opt_value = normalizer(value)
                else:
                    opt_value = str(value)
                args.append(f'--{to_opt}')
                args.append(opt_value)

        # sample rate handling is special; the config value is a list of range-or-values. We have to pick the "best".
        sample_rate = self.best_sample_rate()
        args.append('--sample-rate')
        args.append(str(sample_rate))

        return args

    def best_sample_rate(self) -> int:
        # sample rate handling is special; the config value is a list of range-or-values. We have to pick the "best".
        sample_rate_needed = int(self.channel.width_hz / float(self.config.get('shoulder', 1.0)))
        for sample_rate in self.sample_rates:
            if sample_rate_needed <= sample_rate[1]:
                exact = sample_rate[0] != sample_rate[1]
                logger.debug(f'sample rate {sample_rate_needed} [{sample_rate[0]}-{sample_rate[1]}] (exact? {exact})')
                return sample_rate_needed if exact else sample_rate[1]
        else:
            raise ValueError(f'cannot find an acceptable sample rate for needed width {sample_rate_needed}')

    def observable_channel_widths(self) -> list[int]:
        shoulder = self.config.get('shoulder', 1.0)
        return [int(hi * shoulder) for lo, hi in self.sample_rates]


class RX888mk2Decoder(SoapySDRDecoder):
    def listen_args(self) -> list[str]:
        args = super().listen_args()
        # the quirk here is that RX888mk2 under dumphfdl appears to need centerfrequencies in integral MHz.
        sample_rate = self.best_sample_rate()
        # There is an edge case where rounding to a MHz will put some frequencies into the shoulder. I think this is
        # probably okay. but warn anyways.
        pure_center = self.channel.center_khz
        actual_center = int(round(pure_center / 1000.0) * 1000)
        # check for shoulders.
        shoulder = float(self.config.get('shoulder', 1.0)) * sample_rate / 2.0
        shoulders = [actual_center - shoulder, actual_center + shoulder]
        for freq in self.channel.frequencies:
            if shoulders[0] <= freq <= shoulders[1]:
                logger.debug(f'{freq} is within shoulders')
            else:
                logger.warning(f'{freq} is not within {shoulders} and reception may be impaired')
        args.append('--centerfreq')
        args.append(str(actual_center))
        return args

    def commandline(self) -> list[str]:
        cmd = super().commandline()
        logger.info(f'COMMAND: {cmd}')
        return cmd
