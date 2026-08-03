"""Publishing builds to Meta Horizon Store release channels.

Everything else in Visor talks to a headset over ADB. This package is the one
part that talks to Meta's servers, and it does so by driving `ovr-platform-util`
— the same command line utility Meta Quest Developer Hub drives internally.
That is deliberate: the upload protocol is undocumented, so wrapping Meta's own
tool is the only route that won't rot.
"""

from visor.publish.ovrtool import OvrToolError, OvrToolNotFoundError

__all__ = ["OvrToolError", "OvrToolNotFoundError"]
