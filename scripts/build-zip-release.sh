#!/bin/bash
# This script builds a zipped source release for Moonraker and Klipper.

install_packages()
{
    PKGLIST="python3-dev curl"

    # Update system package info
    report_status "Running apt-get update..."
    sudo apt-get update

    # Install desired packages
    report_status "Installing packages..."
    sudo apt-get install --yes $PKGLIST
}

report_status()
{
    echo -e "\n\n###### $1"
}

verify_ready()
{
    if [ "$EUID" -eq 0 ]; then
        echo "This script must not run as root"
        exit -1
    fi

    if [ ! -d "$SRCDIR/.git" ]; then
        echo "This script must be run from a git repo"
        exit -1
    fi
}

# Force script to exit if an error occurs
set -e

SRCDIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )"/.. && pwd )"
OUTPUT_DIR="$SRCDIR/.dist"
BETA=""

# Parse command line arguments
while getopts "o:b" arg; do
    case $arg in
        o) OUTPUT_DIR=$OPTARG;;
        b) BETA="-b";;
    esac
done

[ ! -d $OUTPUT_DIR ] && mkdir $OUTPUT_DIR
verify_ready
python3 "$SRCDIR/scripts/build_release.py" -o $OUTPUT_DIR $BETA
