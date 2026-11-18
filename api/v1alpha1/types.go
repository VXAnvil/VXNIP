// Package v1alpha1 contains the experimental VMetal inventory and allocation API.
// +groupName=vmetal.infrastructure.cluster.x-k8s.io
// +kubebuilder:object:generate=true
package v1alpha1

import (
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/scheme"
)

var GroupVersion = schema.GroupVersion{
	Group: "vmetal.infrastructure.cluster.x-k8s.io", Version: "v1alpha1",
}

var SchemeBuilder = &scheme.Builder{GroupVersion: GroupVersion}

var InfrastructureGroupVersion = schema.GroupVersion{
	Group: "infrastructure.cluster.x-k8s.io", Version: "v1alpha1",
}

// AddInfrastructureToScheme registers NVKE-facing aliases for the three CAPI
// kinds. Hosts remain in one inventory across both API groups.
func AddInfrastructureToScheme(scheme *runtime.Scheme) {
	scheme.AddKnownTypes(InfrastructureGroupVersion,
		&VMetalMachine{}, &VMetalMachineList{},
		&VMetalCluster{}, &VMetalClusterList{},
		&VMetalMachineTemplate{}, &VMetalMachineTemplateList{},
	)
	metav1.AddToGroupVersion(scheme, InfrastructureGroupVersion)
}

func init() {
	SchemeBuilder.Register(
		&VMetalHost{}, &VMetalHostList{},
		&VMetalMachine{}, &VMetalMachineList{},
		&VMetalCluster{}, &VMetalClusterList{},
		&VMetalMachineTemplate{}, &VMetalMachineTemplateList{},
	)
}

type VMetalHostSpec struct {
	// MachineID is the full machine ID hash reported by the SSH probe.
	// +kubebuilder:validation:Pattern=`^[a-f0-9]{64}$`
	MachineID string `json:"machineID"`

	// SSHTarget is a trusted OpenSSH destination.
	// +kubebuilder:validation:Pattern=`^([a-zA-Z0-9_][a-zA-Z0-9_.-]*@)?[a-zA-Z0-9_][a-zA-Z0-9_.:-]*$`
	// +kubebuilder:validation:MaxLength=255
	SSHTarget string `json:"sshTarget"`

	// AllowBootstrap opts a dedicated lab machine into kubeadm bootstrap and reset.
	AllowBootstrap bool `json:"allowBootstrap,omitempty"`

	// NodeIP is the verified, routable IPv4 address used by NVKE.
	// +kubebuilder:validation:Format=ipv4
	NodeIP string `json:"nodeIP,omitempty"`

	// ManagedAddress allows enrollment to refresh the address of an idle host.
	ManagedAddress bool `json:"managedAddress,omitempty"`
}

type HostClaim struct {
	Namespace string    `json:"namespace"`
	Name      string    `json:"name"`
	UID       types.UID `json:"uid"`
}

type VMetalHostStatus struct {
	Hardware           *apiextensionsv1.JSON `json:"hardware,omitempty"`
	ObservedAt         *metav1.Time          `json:"observedAt,omitempty"`
	ObservedGeneration int64                 `json:"observedGeneration,omitempty"`
	DiscoveryReady     bool                  `json:"discoveryReady,omitempty"`
	// +kubebuilder:validation:Enum=ProbeFailed;IdentityMismatch;Verified;SSHAuthenticationFailed;SSHHostKeyMismatch;SSHTimeout;SSHUnreachable;RemotePrerequisiteFailed;InventoryInvalid
	DiscoveryReason string `json:"discoveryReason,omitempty"`
	// DiscoveryMessage is a credential-free explanation of the latest probe.
	// +kubebuilder:validation:MaxLength=512
	DiscoveryMessage string `json:"discoveryMessage,omitempty"`
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
	Claim      *HostClaim         `json:"claim,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Cluster
// +kubebuilder:subresource:status
// +kubebuilder:validation:XValidation:rule="self.metadata.name == 'host-' + self.spec.machineID.substring(0, 32)",message="name must be host- followed by the first 32 characters of spec.machineID"
// +kubebuilder:validation:XValidation:rule="self.spec == oldSelf.spec || (has(oldSelf.spec.managedAddress) && oldSelf.spec.managedAddress && has(self.spec.managedAddress) && self.spec.managedAddress && self.spec.machineID == oldSelf.spec.machineID && has(self.spec.allowBootstrap) == has(oldSelf.spec.allowBootstrap) && (!has(self.spec.allowBootstrap) || self.spec.allowBootstrap == oldSelf.spec.allowBootstrap) && (!has(oldSelf.status) || !has(oldSelf.status.claim)))",message="only an unclaimed managed host can update its address"
// +kubebuilder:printcolumn:name="Reason",type=string,JSONPath=`.status.discoveryReason`
// +kubebuilder:printcolumn:name="Verified",type=boolean,JSONPath=`.status.discoveryReady`
// +kubebuilder:printcolumn:name="Claim",type=string,JSONPath=`.status.claim.name`
type VMetalHost struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              VMetalHostSpec   `json:"spec"`
	Status            VMetalHostStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type VMetalHostList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []VMetalHost `json:"items"`
}

type VMetalMachineSpec struct {
	// BootstrapMode defaults to the existing Cluster API kubeadm path.
	// +kubebuilder:validation:Enum=Kubeadm;NVKE
	BootstrapMode string `json:"bootstrapMode,omitempty"`

	// ProviderID is written by the controller and matches the kubelet provider ID.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=512
	ProviderID string `json:"providerID,omitempty"`

	// HostRef is the name of a cluster-scoped VMetalHost.
	// +kubebuilder:validation:Pattern=`^host-[a-f0-9]{32}$`
	HostRef string `json:"hostRef"`
}

type VMetalMachineStatus struct {
	// +kubebuilder:validation:Enum=Pending;Reserved;Provisioned
	Phase string `json:"phase,omitempty"`
	// +kubebuilder:validation:Enum=HostMissing;HostDeleting;HostUnverified;HostClaimed;HostReserved;BootstrapDisabled;WaitingForBootstrap;BootstrapRunning;BootstrapComplete;NodeIPUnverified;BootstrapFailed;ResetFailed
	Reason string `json:"reason,omitempty"`
	// Message reports the operation and a credential-free troubleshooting hint.
	// +kubebuilder:validation:MaxLength=512
	Message string `json:"message,omitempty"`

	// BootstrapStarted records intent before any remote changes.
	BootstrapStarted bool                          `json:"bootstrapStarted,omitempty"`
	Initialization   *InfrastructureInitialization `json:"initialization,omitempty"`
	Addresses        []MachineAddress              `json:"addresses,omitempty"`
}

type MachineAddress struct {
	Type    string `json:"type"`
	Address string `json:"address"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:metadata:labels="cluster.x-k8s.io/v1beta2=v1alpha1"
// +kubebuilder:validation:XValidation:rule="self.spec.hostRef == oldSelf.spec.hostRef",message="hostRef is immutable"
// +kubebuilder:validation:XValidation:rule="has(self.spec.bootstrapMode) == has(oldSelf.spec.bootstrapMode) && (!has(self.spec.bootstrapMode) || self.spec.bootstrapMode == oldSelf.spec.bootstrapMode)",message="bootstrapMode is immutable"
// +kubebuilder:printcolumn:name="Host",type=string,JSONPath=`.spec.hostRef`
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Reason",type=string,JSONPath=`.status.reason`
type VMetalMachine struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              VMetalMachineSpec   `json:"spec"`
	Status            VMetalMachineStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type VMetalMachineList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []VMetalMachine `json:"items"`
}
