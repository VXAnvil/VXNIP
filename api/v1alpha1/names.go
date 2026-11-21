package v1alpha1

// VXAnvil names share the existing wire types. Keeping the registered kinds,
// API groups and storage resources unchanged preserves live CAPI references,
// UIDs, claims and finalizers. Do not register a second host inventory.
type VXAnvilHost = VMetalHost
type VXAnvilHostSpec = VMetalHostSpec
type VXAnvilHostStatus = VMetalHostStatus
type VXAnvilHostList = VMetalHostList
type VXAnvilCluster = VMetalCluster
type VXAnvilClusterSpec = VMetalClusterSpec
type VXAnvilClusterStatus = VMetalClusterStatus
type VXAnvilClusterList = VMetalClusterList
type VXAnvilMachine = VMetalMachine
type VXAnvilMachineSpec = VMetalMachineSpec
type VXAnvilMachineStatus = VMetalMachineStatus
type VXAnvilMachineList = VMetalMachineList
type VXAnvilMachineTemplate = VMetalMachineTemplate
type VXAnvilMachineTemplateSpec = VMetalMachineTemplateSpec
type VXAnvilMachineTemplateResource = VMetalMachineTemplateResource
type VXAnvilMachineTemplateList = VMetalMachineTemplateList
