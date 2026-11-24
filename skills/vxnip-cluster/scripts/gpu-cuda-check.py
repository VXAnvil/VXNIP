import ctypes as C,json,uuid
cuda=C.CDLL('libcuda.so.1')
I=C.c_int;U=C.c_uint;P=C.c_void_p;D=C.c_uint64
signatures={
 'cuInit':[U], 'cuDriverGetVersion':[C.POINTER(I)], 'cuDeviceGetCount':[C.POINTER(I)],
 'cuDeviceGet':[C.POINTER(I),I], 'cuDeviceGetName':[P,I,I],
 'cuDeviceComputeCapability':[C.POINTER(I),C.POINTER(I),I],
 'cuDeviceGetUuid':[P,I], 'cuCtxCreate_v2':[C.POINTER(P),U,I],
 'cuMemAlloc_v2':[C.POINTER(D),C.c_size_t], 'cuModuleLoadData':[C.POINTER(P),P],
 'cuModuleGetFunction':[C.POINTER(P),P,C.c_char_p],
 'cuLaunchKernel':[P,U,U,U,U,U,U,U,P,C.POINTER(P),C.POINTER(P)],
 'cuCtxSynchronize':[], 'cuMemcpyDtoH_v2':[P,D,C.c_size_t],
 'cuMemFree_v2':[D], 'cuModuleUnload':[P], 'cuCtxDestroy_v2':[P],
 'cuGetErrorName':[I,C.POINTER(C.c_char_p)]}
for name,args in signatures.items():
 fn=getattr(cuda,name);fn.argtypes=args;fn.restype=I

def call(name,*args):
 result=getattr(cuda,name)(*args)
 if result:
  label=C.c_char_p();cuda.cuGetErrorName(result,C.byref(label))
  raise RuntimeError(name+': '+str(result)+' '+str(label.value))

call('cuInit',0)
count=I();call('cuDeviceGetCount',C.byref(count));assert count.value==1,count.value
dev=I();call('cuDeviceGet',C.byref(dev),0)
name=C.create_string_buffer(128);call('cuDeviceGetName',name,128,dev)
major=I();minor=I();call('cuDeviceComputeCapability',C.byref(major),C.byref(minor),dev)
id_buffer=C.create_string_buffer(16);call('cuDeviceGetUuid',id_buffer,dev)
version=I();call('cuDriverGetVersion',C.byref(version))
context=P();module=P();memory=D()
try:
 call('cuCtxCreate_v2',C.byref(context),0,dev)
 call('cuMemAlloc_v2',C.byref(memory),4)
 ptx_version,ptx_target=(('8.0','sm_90') if major.value>=9 else ('7.0','sm_80') if major.value>=8 else ('6.0','sm_70') if major.value>=7 else ('4.2','sm_52'))
 ptx=C.create_string_buffer(('''.version VERSION
.target TARGET
.address_size 64
.visible .entry write_answer(.param .u64 output) {
 .reg .u64 %rd;
 .reg .u32 %r;
 ld.param.u64 %rd, [output];
 mov.u32 %r, 42;
 st.global.u32 [%rd], %r;
 ret;
}
'''.replace('VERSION',ptx_version).replace('TARGET',ptx_target)).encode())
 call('cuModuleLoadData',C.byref(module),C.cast(ptx,P))
 kernel=P();call('cuModuleGetFunction',C.byref(kernel),module,b'write_answer')
 args=(P*1)(C.cast(C.byref(memory),P))
 call('cuLaunchKernel',kernel,1,1,1,1,1,1,0,None,args,None)
 call('cuCtxSynchronize')
 answer=C.c_uint32();call('cuMemcpyDtoH_v2',C.byref(answer),memory,4)
 assert answer.value==42,answer.value
 print(json.dumps({'cuda_kernel_passed':True,'result':answer.value,'gpu':name.value.decode(),'gpu_uuid':'GPU-'+str(uuid.UUID(bytes=id_buffer.raw)),'compute_capability':str(major.value)+'.'+str(minor.value),'cuda_driver_api':version.value}),flush=True)
finally:
 if memory.value:cuda.cuMemFree_v2(memory)
 if module.value:cuda.cuModuleUnload(module)
 if context.value:cuda.cuCtxDestroy_v2(context)
