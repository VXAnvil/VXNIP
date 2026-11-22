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

type VXNIPClusterSpec struct {
	ControlPlaneEndpoint APIEndpoint `json:"controlPlaneEndpoint"`
}

type VXNIPClusterStatus struct {
	Initialization *InfrastructureInitialization `json:"initialization,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:metadata:labels="cluster.x-k8s.io/v1beta2=v1alpha1"
// +kubebuilder:validation:XValidation:rule="self.spec == oldSelf.spec",message="the control plane endpoint is immutable"
type VXNIPCluster struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              VXNIPClusterSpec   `json:"spec"`
	Status            VXNIPClusterStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type VXNIPClusterList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []VXNIPCluster `json:"items"`
}

type VXNIPMachineTemplateSpec struct {
	Template VXNIPMachineTemplateResource `json:"template"`
}

type VXNIPMachineTemplateResource struct {
	Spec VXNIPMachineSpec `json:"spec"`
}

// +kubebuilder:object:root=true
// +kubebuilder:metadata:labels="cluster.x-k8s.io/v1beta2=v1alpha1"
// +kubebuilder:validation:XValidation:rule="self.spec == oldSelf.spec",message="machine template is immutable"
type VXNIPMachineTemplate struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              VXNIPMachineTemplateSpec `json:"spec"`
}

// +kubebuilder:object:root=true
type VXNIPMachineTemplateList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []VXNIPMachineTemplate `json:"items"`
}
