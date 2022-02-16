from __future__ import annotations

import asyncio
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path
from typing import Literal, NamedTuple

import numpy as np
from tifffile import TiffWriter

from .base.instruments_types import SerialPorts
from .imaging.camera.dcam import Cameras, UInt16Array
from .imaging.fpga import FPGA
from .imaging.laser import Laser, Lasers
from .imaging.xstage import XStage
from .imaging.ystage import YStage

logger = getLogger(__name__)


class State(NamedTuple):
    x: int
    y: int
    z_tilt: tuple[int, int, int]
    z_obj: int
    laser_g: int
    laser_r: int


# Due to the optical arrangement, the actual channel ordering
# is not in order of increasing wavelength.
CHANNEL = {0: 1, 1: 3, 2: 2, 3: 0}


class Imager:
    UM_PER_PX = 0.375

    @classmethod
    async def ainit(cls, ports: dict[SerialPorts, str], init_cam: bool = True) -> Imager:
        to_init = (
            FPGA.ainit(ports["fpgacmd"], ports["fpgaresp"]),
            XStage.ainit(ports["x"]),
            YStage.ainit(ports["y"]),
            Laser.ainit("g", ports["laser_g"]),
            Laser.ainit("r", ports["laser_r"]),
        )

        if init_cam:
            fpga, x, y, laser_g, laser_r, cams = await asyncio.gather(*to_init, Cameras.ainit())
            return cls(fpga, x, y, Lasers(g=laser_g, r=laser_r), cams)

        fpga, x, y, laser_g, laser_r = await asyncio.gather(*to_init)
        return cls(fpga, x, y, Lasers(g=laser_g, r=laser_r), cams=None)

    def __init__(self, fpga: FPGA, x: XStage, y: YStage, lasers: Lasers, cams: Cameras | None) -> None:
        self.fpga = fpga
        self.tdi = self.fpga.tdi
        self.optics = self.fpga.optics
        self.lasers = lasers

        self.x = x
        self.y = y
        self.z_tilt = self.fpga.z_tilt
        self.z_obj = self.fpga.z_obj

        self.cams = cams
        self.lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self.lock:
            logger.info("Starting imager initialization.")
            await asyncio.gather(
                self.x.initialize(),
                self.y.initialize(),
                self.z_tilt.initialize(),
                self.z_obj.initialize(),
                self.optics.initialize(),
            )
            logger.info("Imager initialization completed.")

    @property
    async def state(self) -> State:
        names = {
            "x": self.x.pos,
            "y": self.y.pos,
            "z_tilt": self.z_tilt.pos,
            "z_obj": self.z_obj.pos,
            "laser_g": self.lasers.g.power,
            "laser_r": self.lasers.r.power,
        }
        res = await asyncio.gather(*names.values())
        return State(**dict(zip(names.keys(), res)))

    async def wait_ready(self) -> None:
        """Returns when no commands are pending return which indicates that all motors are idle.
        This is because all move commands are expected to return some value upon completion.
        """
        logger.info("Waiting for all motions to complete.")
        await self.x.com.wait()
        await self.y.com.wait()
        await self.fpga.com.wait()
        logger.info("All motions completed.")

    async def take(
        self,
        n_bundles: int,
        dark: bool = False,
        channels: frozenset[Literal[0, 1, 2, 3]] = frozenset((0, 1, 2, 3)),
        move_back_to_start: bool = True,
    ) -> tuple[UInt16Array, State]:
        assert self.cams is not None
        if self.lock.locked():
            logger.info("Waiting for the previous imaging operation to complete.")

        async with self.lock:
            if not 0 < n_bundles < 1500:
                raise ValueError("n_bundles should be between 0 and 1500.")

            logger.info(f"Taking an image with {n_bundles} from channel(s) {channels}.")

            c = [CHANNEL[x] for x in sorted(channels)]
            if (0 in c or 1 in c) and (2 in c or 3 in c):
                cam = 2
            elif 0 in c or 1 in c:
                cam = 0
            elif 2 in c or 3 in c:
                cam = 1
            else:
                raise ValueError("Invalid channel(s).")

            # TODO: Compensate actual start position.
            n_bundles += 1  # To flush CCD.
            await self.wait_ready()

            state = await self.state
            pos = state.y
            n_px_y = n_bundles * self.cams.BUNDLE_HEIGHT
            # Need overshoot for TDI to function properly.
            end_y_pos = pos - self.calc_delta_pos(n_px_y) - 100000
            assert end_y_pos > -7e6

            await asyncio.gather(self.tdi.prepare_for_imaging(n_px_y, pos), self.y.set_mode("IMAGING"))
            cap = self.cams.acapture(n_bundles, fut_capture=self.y.move(end_y_pos, slowly=True), cam=cam)

            if dark:
                imgs = await cap
            else:
                async with self.optics.open_shutter():
                    imgs = await cap

            logger.info(f"Done taking an image.")

            await self.y.move(pos if move_back_to_start else end_y_pos + 100000)  # Correct for overshoot.
            imgs = np.clip(np.flip(imgs, axis=1), 0, 4096)
            if cam == 1:
                return imgs[[x - 2 for x in c], :-128, :]  # type: ignore
            return imgs[:, :-128, :], state  # Remove oversaturated first bundle.

    @staticmethod
    def calc_delta_pos(n_px_y: int) -> int:
        return int(n_px_y * Imager.UM_PER_PX * YStage.STEPS_PER_UM)

    async def autofocus(self, channel: Literal[0, 1, 2, 3] = 1) -> tuple[int, UInt16Array]:
        """Moves to z_max and takes 232 (2048 × 5) images while moving to z_min.
        Returns the z position of maximum intensity and the images.
        """
        assert self.cams is not None
        if self.lock.locked():
            logger.info("Waiting for the previous imaging operation to complete.")

        async with self.lock:
            logger.info(f"Starting autofocus using data from {channel=}.")
            if channel not in (0, 1, 2, 3):
                raise ValueError(f"Invalid channel {channel}.")

            await self.wait_ready()

            n_bundles, height = 232, 5
            z_min, z_max = 2621, 60292
            cam = 0 if CHANNEL[channel] in (0, 1) else 1

            async with self.z_obj.af_arm(z_min=z_min, z_max=z_max) as start_move:
                async with self.optics.open_shutter():
                    img = await self.cams.acapture(
                        n_bundles, height, fut_capture=start_move, mode="FOCUS_SWEEP", cam=cam
                    )

            intensity = np.mean(
                np.reshape(img[CHANNEL[channel] - 2 * cam], (n_bundles, height, 2048)), axis=(1, 2)
            )
            target = int(z_max - (((z_max - z_min) / n_bundles) * np.argmax(intensity) + z_min))
            logger.info(f"Done autofocus. Optimum={target}")
            if not 10000 < target < 50000:
                logger.info(f"Target too close to edge, considering moving the tilt motors.")
            return (target, intensity)

    @staticmethod
    def save_image(path: str | Path, img: UInt16Array, state: State) -> None:
        """
        Based on 2016-06
        http://www.openmicroscopy.org/Schemas/Documentation/Generated/OME-2016-06/ome.html

        Focus on the Image/Pixel attribute.
        https://github.com/cgohlke/tifffile/issues/92#issuecomment-879309911
        TODO: Find some way to embed metadata in the OME-XML https://github.com/cgohlke/tifffile/issues/65

        Args:
            path (str | Path): _description_
            img (UInt16Array): _description_
            state (State): _description_
        """
        if isinstance(path, Path):
            path = path.as_posix()

        if not (path.endswith(".tif") or path.endswith(".tiff")):
            logger.warning("File name does not end with a tif extension.")

        try:
            with TiffWriter(path, ome=True) as tif:
                tif.write(
                    img,
                    compression="ZLIB",
                    resolution=(1 / (0.375 * 10**-4), 1 / (0.375 * 10**-4), "CENTIMETER"),  # 0.375 μm/px
                    metadata={"axes": "CYX", "SignificantBits": 12},
                )
        except BaseException as e:
            logger.error(f"Exception {e}")