#!/bin/sh
__doc__="
FreeBSD often runs into issues when we make new releases.
This script contains notes on how to get a working test
environment quickly on a fresh VM install.

References:
    https://github.com/pyutils/line_profiler/issues/266


When making the VM give it more than 1 CPU to make compiling things faster.

To make editing easier, we can ssh into the virtualmachine:
    https://superuser.com/questions/597280/how-to-adjust-screen-size-for-virtualbox-vms

    Enable port forwarding on virtualbox network
    https://dev.to/developertharun/easy-way-to-ssh-into-virtualbox-machine-any-os-just-x-steps-5d9i

    setup a port forward from 2222 on host to 22 on the VM

    In the VM

    vi /etc/ssh/sshd_config
    # EDIT TO enable password auth
    # Add: PermitRootLogin yes
    # Add: PasswordAuthentication yes
    # Add: PermitEmptyPasswords yes

    /etc/rc.d/sshd restart

    # Then:
    ssh -p 2222 root@localhost
"

pkg update
pkg install git
git clone --depth 1 https://git.FreeBSD.org/ports.git /usr/ports
cd /usr/ports/devel/py-line-profiler

# Wow this step takes a long time, and seems to be interactive.
# Is there a way to make it non-interactive?
pkg install -A `make missing`

# This next step also takes a long time
make

# This step also has interactive pieces
make test



pkg install py39-pip
git clone https://github.com/pyutils/line_profiler.git
cd line_profiler
pip install -e .[tests-strict]
pytest


cd /usr/ports/devel/py-line-profiler/work-py39/line_profiler-4.1.3 && \
/usr/bin/env -i HOME=/usr/ports/devel/py-line-profiler/work-py39  \
PWD="${PWD}"  \
__MAKE_CONF=/nonexistent \
OSVERSION=1400509 \
PATH=/usr/local/libexec/ccache:/usr/ports/devel/py-line-profiler/work-py39/.bin:/home/yuri/bin:/sbin:/bin:/usr/sbin:/usr/bin:/usr/local/sbin:/usr/local/bin \
TERM=xterm-256color XDG_DATA_HOME=/usr/ports/devel/py-line-profiler/work-py39 \
XDG_CONFIG_HOME=/usr/ports/devel/py-line-profiler/work-py39 \
XDG_CACHE_HOME=/usr/ports/devel/py-line-profiler/work-py39/.cache \
HOME=/usr/ports/devel/py-line-profiler/work-py39 \
PATH=/usr/local/libexec/ccache:/usr/ports/devel/py-line-profiler/work-py39/.bin:/home/yuri/bin:/sbin:/bin:/usr/sbin:/usr/bin:/usr/local/sbin:/usr/local/bin \
PKG_CONFIG_LIBDIR=/usr/ports/devel/py-line-profiler/work-py39/.pkgconfig:/usr/local/libdata/pkgconfig:/usr/local/share/pkgconfig:/usr/libdata/pkgconfig \
MK_DEBUG_FILES=no \
MK_KERNEL_SYMBOLS=no \
SHELL=/bin/sh NO_LINT=YES LDSHARED="cc -shared" \
PYTHONDONTWRITEBYTECODE= PYTHONOPTIMIZE= \
PREFIX=/usr/local \
LOCALBASE=/usr/local \
CC="cc" \
CFLAGS="-O2 -pipe  -fstack-protector-strong -fno-strict-aliasing "  \
CPP="cpp" CPPFLAGS=""  LDFLAGS=" -fstack-protector-strong " LIBS=""  CXX="c++" \
CXXFLAGS="-O2 -pipe -fstack-protector-strong -fno-strict-aliasing  " \
CCACHE_DIR="/tmp/.ccache" \
BSD_INSTALL_PROGRAM="install  -s -m 555"  \
BSD_INSTALL_LIB="install  -s -m 0644" \
BSD_INSTALL_SCRIPT="install  -m 555"  \
BSD_INSTALL_DATA="install  -m 0644"  \
BSD_INSTALL_MAN="install  -m 444" \
PYTHONPATH=/usr/ports/devel/py-line-profiler/work-py39/stage/usr/local/lib/python3.9/site-packages \
/usr/local/bin/python3.9 -m pytest -p no:xdist -p no:cov -k '' -rs -v -o addopts=




/usr/local/bin/python3.9 -m pip uninstall pytest
/usr/local/bin/python3.9 -m pip uninstall pytest
pip install "pytest==7.4.4"
pip install anyio
pip install "pytest-checkdocs==2.12.0"
pip install "pytest==7.4.4"
pip install "typeguard==4.2.1"
pip install "hypothesis==6.98.18"
pip install "pytest-cov==4.1.0"
pip install "pytest-enabler==3.1.1" "pytest-xdist==3.5.0"
/usr/local/bin/python3.9 -m pytest --help
