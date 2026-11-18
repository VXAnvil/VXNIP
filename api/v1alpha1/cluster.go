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

type HomelabClusterSpec struct {
	ControlPlaneEndpoint APIEndpoint `json:"controlPlaneEndpoint"`
}

type HomelabClusterStatus struct {
	Initialization *InfrastructureInitialization `json:"initialization,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:metadata:labels="cluster.x-k8s.io/v1beta2=v1alpha1"
// +kubebuilder:validation:XValidation:rule="self.spec == oldSelf.spec",message="the control plane endpoint is immutable"
type HomelabCluster struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              HomelabClusterSpec   `json:"spec"`
	Status            HomelabClusterStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type HomelabClusterList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []HomelabCluster `json:"items"`
}

type HomelabMachineTemplateSpec struct {
	Template HomelabMachineTemplateResource `json:"template"`
}

type HomelabMachineTemplateResource struct {
	Spec HomelabMachineSpec `json:"spec"`
}

// +kubebuilder:object:root=true
// +kubebuilder:metadata:labels="cluster.x-k8s.io/v1beta2=v1alpha1"
// +kubebuilder:validation:XValidation:rule="self.spec == oldSelf.spec",message="machine template is immutable"
type HomelabMachineTemplate struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              HomelabMachineTemplateSpec `json:"spec"`
}

// +kubebuilder:object:root=true
type HomelabMachineTemplateList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []HomelabMachineTemplate `json:"items"`
}
