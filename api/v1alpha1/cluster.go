package v1alpha1

import metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

type InfrastructureInitialization struct {
	Provisioned bool `json:"provisioned"`
}

type APIEndpoint struct {
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=253
	Host string `json:"host"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=65535
	Port int32 `json:"port"`
}

type VMetalClusterSpec struct {
	ControlPlaneEndpoint APIEndpoint `json:"controlPlaneEndpoint"`
}

type VMetalClusterStatus struct {
	Initialization *InfrastructureInitialization `json:"initialization,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:metadata:labels="cluster.x-k8s.io/v1beta2=v1alpha1"
// +kubebuilder:validation:XValidation:rule="self.spec == oldSelf.spec",message="the control plane endpoint is immutable"
type VMetalCluster struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              VMetalClusterSpec   `json:"spec"`
	Status            VMetalClusterStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type VMetalClusterList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []VMetalCluster `json:"items"`
}

type VMetalMachineTemplateSpec struct {
	Template VMetalMachineTemplateResource `json:"template"`
}

type VMetalMachineTemplateResource struct {
	Spec VMetalMachineSpec `json:"spec"`
}

// +kubebuilder:object:root=true
// +kubebuilder:metadata:labels="cluster.x-k8s.io/v1beta2=v1alpha1"
// +kubebuilder:validation:XValidation:rule="self.spec == oldSelf.spec",message="machine template is immutable"
type VMetalMachineTemplate struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              VMetalMachineTemplateSpec `json:"spec"`
}

// +kubebuilder:object:root=true
type VMetalMachineTemplateList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []VMetalMachineTemplate `json:"items"`
}
