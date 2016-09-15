#!/bin/bash
# Live migration dedicated ci job will be responsible for testing different
# environments based on underlying storage, used for ephemerals.
# This hook allows to inject logic of environment reconfiguration in ci job.
# Base scenario for this would be:
#
# 1. test with all local storage (use default for volumes)
# 2. test with NFS for root + ephemeral disks
# 3. test with Ceph for root + ephemeral disks
# 4. test with Ceph for volumes and root + ephemeral disk

set -xe
cd $BASE/new/tempest

source $BASE/new/devstack/functions
source $BASE/new/devstack/functions-common
source $WORKSPACE/devstack-gate/functions.sh
source $BASE/new/nova/nova/tests/live_migration/hooks/utils.sh
source $BASE/new/nova/nova/tests/live_migration/hooks/nfs.sh
source $BASE/new/nova/nova/tests/live_migration/hooks/ceph.sh
primary_node=$(cat /etc/nodepool/primary_node_private)
SUBNODES=$(cat /etc/nodepool/sub_nodes_private)
SERVICE_HOST=$primary_node
STACK_USER=${STACK_USER:-stack}

populate_start_script

echo '1. test with all local storage (use default for volumes)'
echo 'NOTE: test_volume_backed_live_migration is skipped due to https://bugs.launchpad.net/nova/+bug/1524898'
run_tempest "block migration test" "^.*test_live_migration(?!.*(test_volume_backed_live_migration))"

echo '2. NFS testing is skipped due to setup failures with Ubuntu 16.04'
#echo '2. test with NFS for root + ephemeral disks'

#nfs_setup
#nfs_configure_tempest
#nfs_verify_setup
#run_tempest  "NFS shared storage test" "live_migration"
#nfs_teardown

echo '3. Ceph testing is skipped due to setup failures with Ubuntu 16.04'
#echo '3. test with Ceph for root + ephemeral disks'

#source $BASE/new/devstack/lib/ceph

#reset output
#set -xe

#setup_ceph_cluster
#configure_and_start_glance
#configure_and_start_nova
#run_tempest "Ceph nova&glance test" "live_migration"

#echo '4. test with Ceph for volumes and root + ephemeral disk'

#configure_and_start_cinder
#run_tempest "Ceph nova&glance&cinder test" "live_migration"