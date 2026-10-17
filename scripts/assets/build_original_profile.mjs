/** Fit the supplied full 3D stone to the original photograph's front silhouette. */
import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { readFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import * as THREE from 'three'
const require=createRequire(resolve(process.env.MOYAI_ASSET_TOOLS||'.','package.json'))
const {NodeIO}=require('@gltf-transform/core')
const {transformPrimitive,prune}=require('@gltf-transform/functions')
const sharp=require('sharp')
const [input,reference,textureDirectory,output]=process.argv.slice(2)
if(!output)throw new Error('Usage: build_original_profile.mjs original-meshy.glb reference.webp texture-directory output.glb')
if(createHash('sha256').update(await readFile(input)).digest('hex')!=='af74b964b07d954a156893f441850e7e72186d11baa0c854e9f17d21e2536a05')throw new Error('Expected the unmodified supplied Meshy conversion.')
const D=8.6,BINS=720
const source=await sharp(reference).ensureAlpha().raw().toBuffer({resolveWithObject:true})
let left=source.info.width,top=source.info.height,right=0,bottom=0
for(let y=0;y<source.info.height;y++)for(let x=0;x<source.info.width;x++)if(source.data[(y*source.info.width+x)*4+3]){left=Math.min(left,x);right=Math.max(right,x+1);top=Math.min(top,y);bottom=Math.max(bottom,y+1)}
const objectHeight=bottom-top
left=Math.max(left-8,0);top=Math.max(top-8,0);right=Math.min(right+8,source.info.width);bottom=Math.min(bottom+8,source.info.height)
const crop={left,top,width:right-left,height:bottom-top}
const worldHeight=4.15*crop.height/objectHeight,worldWidth=worldHeight*crop.width/crop.height
const photo=await sharp(reference).extract(crop).png().toBuffer()
const pixels=await sharp(photo).ensureAlpha().raw().toBuffer()
const target=new Float64Array(BINS),sourceRadius=new Float64Array(BINS)
const bin=(x,y)=>Math.round(((Math.atan2(y,x)+Math.PI)/(2*Math.PI))*BINS)%BINS
for(let y=0;y<crop.height;y++)for(let x=0;x<crop.width;x++)if(pixels[(y*crop.width+x)*4+3]>48){const sx=(x/(crop.width-1)-.5)*worldWidth,sy=(.5-y/(crop.height-1))*worldHeight;const j=bin(sx,sy);target[j]=Math.max(target[j],Math.hypot(sx,sy))}
function fill(a){for(let i=0;i<BINS;i++)if(!a[i]){let p=1,n=1;while(!a[(i-p+BINS)%BINS])p++;while(!a[(i+n)%BINS])n++;a[i]=(a[(i-p+BINS)%BINS]*n+a[(i+n)%BINS]*p)/(p+n)}}
fill(target)
const io=new NodeIO(),doc=await io.read(input),root=doc.getRoot(),node=root.listNodes().find(n=>n.getMesh()),primitive=node.getMesh().listPrimitives()[0]
transformPrimitive(primitive,node.getWorldMatrix())
for(const n of root.listNodes()){n.setTranslation([0,0,0]);n.setRotation([0,0,0,1]);n.setScale([1,1,1])}
const p=primitive.getAttribute('POSITION').getArray(),count=p.length/3,index=primitive.getIndices().getArray()
const rotation=new THREE.Matrix4().makeRotationZ(.15).multiply(new THREE.Matrix4().makeRotationY(.42)),v=new THREE.Vector3()
for(let i=0;i<count;i++){v.fromArray(p,i*3).applyMatrix4(rotation);v.toArray(p,i*3);const x=v.x*D/(D-v.z),y=v.y*D/(D-v.z),j=bin(x,y);sourceRadius[j]=Math.max(sourceRadius[j],Math.hypot(x,y))}
fill(sourceRadius)
const coarse=new Float64Array(BINS)
for(let j=0;j<BINS;j++){let a=0,b=0;for(let d=-18;d<=18;d++){a+=target[(j+d+BINS)%BINS];b+=sourceRadius[(j+d+BINS)%BINS]}coarse[j]=a/b}
const uv=new Float32Array(count*2)
for(let i=0;i<count;i++){const k=i*3,z=p[k+2],x=p[k]*D/(D-z),y=p[k+1]*D/(D-z),j=bin(x,y);let sourceMax=0;for(let d=-2;d<=2;d++)sourceMax=Math.max(sourceMax,sourceRadius[(j+d+BINS)%BINS]);let edge=Math.max(0,Math.min(1,(Math.hypot(x,y)/sourceMax-.72)/.25));edge=edge*edge*(3-2*edge);const scale=coarse[j]*(1-edge)+target[j]*1.015/sourceMax*edge;const sx=x*scale,sy=y*scale;p[k]=sx*(D-z)/D;p[k+1]=sy*(D-z)/D;uv[i*2]=sx/worldWidth+.5;uv[i*2+1]=.5-sy/worldHeight}
const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.BufferAttribute(p,3));geometry.setIndex(new THREE.BufferAttribute(index,1));geometry.computeVertexNormals()
primitive.getAttribute('POSITION').setArray(p)
primitive.getAttribute('NORMAL').setArray(geometry.getAttribute('normal').array)
primitive.setAttribute('TANGENT',null)
primitive.setAttribute('TEXCOORD_1',doc.createAccessor('Original photograph projection').setType('VEC2').setArray(uv).setBuffer(root.listBuffers()[0]))
const material=primitive.getMaterial(),stem=resolve(textureDirectory,'Meshy_AI_Silent_Stone_Orbit_1017192604_texture')
for(const [texture,path] of [[material.getBaseColorTexture(),stem+'.png'],[material.getNormalTexture(),stem+'_normal.png']])texture.setImage(await sharp(path).flip().jpeg({quality:92,chromaSubsampling:'4:4:4'}).toBuffer()).setMimeType('image/jpeg')
const referenceTexture=doc.createTexture('Original front photograph').setImage(photo).setMimeType('image/png')
// Carries the reference image in standard glTF; the viewer uses it as a front overlay.
material.setEmissiveTexture(referenceTexture).setEmissiveFactor([0,0,0])
material.getEmissiveTextureInfo().setTexCoord(1)
material.setName('Original silhouette with full 3D profile')
await doc.transform(prune())
await io.write(output,doc)
console.log(JSON.stringify({vertices:count,triangles:index.length/3,worldHeight,worldWidth,referenceCrop:crop},null,2))
