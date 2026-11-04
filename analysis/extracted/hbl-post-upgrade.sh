busybox sed 's/slot_\([0-9]\).*/\1/') # ToyBox sed fails
upgraded_base=/blackbox/upgraded/hbl
persistent_storage=/data
upgrade_files="$upgraded_base $persistent_storage/hbl-retry-upgrade $persistent_storage/hbl-post-upgrade"
hbmanual_pack="$upgraded_base/hbmanual_pack.tar"
hbmanual_dev="/dev/block/platform/soc/by-name/hbmanual"

logdir=/blackbox/upgrade
mkdir -p $logdir
logcat -d > /$logdir/post_upgrade.log

if [ "$boot_expected" = "$boot_active" ]; then
     echo "Upgrade successful, booting to system $boot_active"
     rm $logdir/upgrade-*.log
     exit_status=0
else
     echo "Upgrade failed, booting from $boot_active expected $boot_expected"
     # Restore CPLD if marked as dirty
     if [ -f $persistent_storage/cpld-dirty ]; then
          /system/bin/upgrade_cpld.sh
          rm $persistent_storage/cpld-dirty
          reboot
     fi
fi

/system/bin/fixup_hbmanual_partition.sh "$hbmanual_pack"

# Properties that must be fixed for the user
odindb-send -s config -p temperature_warn_sensor 51

unrd -d hbl_upgrading
# Make sure to clear the crash counter when we are not upgrading anymore
unrd -d crash_counter
# Mark the partition bootable
boot_control -m
rm -rf $upgrade_files

exit $exit_status
ù–ÿo4w‹MIİ×W}yĞZ.•ø‡º5S5ø-¢Xşa×¢ÖğÔZáàŸ(Aû_Á6~"î€²×âöíÃª}¾^fîš°„‡{Õ&?$iÒB ™™x^U½âÛtÀ´~<™s°>î×±Àl’IO¼Kÿk¹A