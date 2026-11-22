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

type VXAnvilClusterSpec struct {
	ControlPlaneEndpoint APIEndpoint `json:"controlPlaneEndpoint"`
}

type VXAnvilClusterStatus struct {
	Initialization *InfrastructureInitialization `json:"initialization,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:metadata:labels="cluster.x-k8s.io/v1beta2=v1alpha1"
// +kubebuilder:validation:XValidation:rule="self.spec == oldSelf.spec",message="the control plane endpoint is immutable"
type VXAnvilCluster struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              VXAnvilClusterSpec   `json:"spec"`
	Status            VXAnvilClusterStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type VXAnvilClusterList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []VXAnvilCluster `json:"items"`
}

type VXAnvilMachineTemplateSpec struct {
	Template VXAnvilMachineTemplateResource `json:"template"`
}

type VXAnvilMachineTemplateResource struct {
	Spec VXAnvilMachineSpec `json:"spec"`
}

// +kubebuilder:object:root=true
// +kubebuilder:metadata:labels="cluster.x-k8s.io/v1beta2=v1alpha1"
// +kubebuilder:validation:XValidation:rule="self.spec == oldSelf.spec",message="machine template is immutable"
type VXAnvilMachineTemplate struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              VXAnvilMachineTemplateSpec `json:"spec"`
}

// +kubebuilder:object:root=true
type VXAnvilMachineTemplateList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []VXAnvilMachineTemplate `json:"items"`
}
