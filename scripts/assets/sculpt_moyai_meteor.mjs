/** Offline art pass. See moyai-model.md for source inputs and pinned tool versions. */
import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { readFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'

const require = createRequire(resolve(process.env.MOYAI_ASSET_TOOLS || '.', 'package.json'))
const { NodeIO } = require('@gltf-transform/core')
const { transformPrimitive, prune } = require('@gltf-transform/functions')
const sharp = require('sharp')
const [input, textures, output] = process.argv.slice(2)
if (!input || !textures || !output) throw new Error('Usage: sculpt_moyai_meteor.mjs source.glb texture-directory output.glb')
const inputHash = createHash('sha256').update(await readFile(input)).digest('hex')
if (inputHash !== 'af74b964b07d954a156893f441850e7e72186d11baa0c854e9f17d21e2536a05') {
  throw new Error('Use the original optimized Silent Stone Orbit GLB; reapplying this pass would deform the model twice.')
}
const io = new NodeIO(), doc = await io.read(input), root = doc.getRoot()
const node = root.listNodes().find(n => n.getMesh())
const primitive = node.getMesh().listPrimitives()[0]
transformPrimitive(primitive, node.getWorldMatrix())
for (const n of root.listNodes()) { n.setTranslation([0, 0, 0]); n.setRotation([0, 0, 0, 1]); n.setScale([1, 1, 1]) }
const position = primitive.getAttribute('POSITION').getArray()
const index = primitive.getIndices().getArray(), count = position.length / 3
const smooth = (a, b, x) => { const t = Math.max(0, Math.min(1, (x - a) / (b - a))); return t*t*(3-2*t) }
const faceMask = (x, y, z) => (1-smooth(.70,.94,Math.abs(x))) * (1-smooth(1.03,1.36,Math.abs(y+.05))) * smooth(.45,.80,z)
const faces = new Float32Array(count)
const angleDistance = (a,b) => Math.atan2(Math.sin(a-b), Math.cos(a-b))
// Broad shelves, narrow peaks and deliberate gaps retain the source's broken edges.
const ridges = [[1.96,.105,.19],[2.73,.045,.31],[-2.30,.09,.22],[-.68,-.055,.29],[.43,.065,.18],[1.02,-.035,.23]]
for (let i=0; i<count; i++) {
  const k=i*3, x=position[k], y=position[k+1], z=position[k+2]
  const protect=faceMask(x,y,z), shell=1-protect; faces[i]=protect
  const theta=Math.atan2(y/1.66,x/1.75)
  const rim=smooth(.50,.90,Math.hypot(x/1.75,y/1.66))
  const peak=ridges.reduce((sum,[angle,height,width])=>sum+height*Math.exp(-Math.pow(angleDistance(theta,angle)/width,2)),0)*rim
  // A yawed ellipsoid adds depth in three-quarter views without stretching the face.
  const angle=.30, c=Math.cos(angle), s=Math.sin(angle)
  const u=c*x-s*z, v=s*x+c*z
  const expandedX=c*u*1.16+s*v*1.34
  const expandedZ=-s*u*1.16+c*v*1.34
  position[k]=x+shell*(expandedX-x-.14*y+x*peak)
  position[k+1]=y+shell*(y*.22+y*peak)
  position[k+2]=z+shell*(expandedZ-z+z*peak*.55)
}
// Weld adjacency by position so cavity masks and smooth normals cross UV seams.
const groups=[], groupOf=new Uint32Array(count), keys=new Map()
for(let i=0;i<count;i++) {
  const k=i*3,key=[position[k],position[k+1],position[k+2]].map(v=>Math.round(v*1e5)).join(',')
  let g=keys.get(key);if(g===undefined){g=groups.length;keys.set(key,g);groups.push({p:[position[k],position[k+1],position[k+2]],n:[0,0,0],adj:new Set(),face:faces[i]})}
  groupOf[i]=g;groups[g].face=Math.max(groups[g].face,faces[i])
}
for(let i=0;i<index.length;i+=3) {
  const ids=[groupOf[index[i]],groupOf[index[i+1]],groupOf[index[i+2]]]
  const [a,b,c]=ids.map(id=>groups[id].p)
  const u=b.map((v,j)=>v-a[j]), v=c.map((v,j)=>v-a[j])
  const n=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]]
  for(const g of ids){for(let j=0;j<3;j++)groups[g].n[j]+=n[j];for(const other of ids)if(g!==other)groups[g].adj.add(other)}
}
let cavity=new Float32Array(groups.length)
for(let i=0;i<groups.length;i++) {
  const g=groups[i], length=Math.hypot(...g.n)||1;g.n=g.n.map(v=>v/length)
  let delta=0,distance=0
  for(const id of g.adj){const d=groups[id].p.map((v,j)=>v-g.p[j]);delta+=d.reduce((sum,v,j)=>sum+v*g.n[j],0);distance+=d.reduce((sum,v)=>sum+v*v,0)}
  cavity[i]=smooth(.75,4.6,delta/Math.max(distance,1e-10))*(1-g.face)
}
for(let pass=0;pass<5;pass++) {
  const next=new Float32Array(cavity.length)
  for(let i=0;i<groups.length;i++){const g=groups[i];let sum=0;for(const id of g.adj)sum+=cavity[id];next[i]=(cavity[i]*.55+sum/Math.max(g.adj.size,1)*.45)*(1-g.face)}
  cavity=next
}
const normal=new Float32Array(count*3), mask=new Float32Array(count)
// Art-directed bowl centers in the deformed mesh, checked from front and 3/4 views.
// Curvature alone also finds folds between boulders; those get only a faint accent.
const craters = [
  [-.908,.664,.206,.46,-.668,.028,.743],
  [-.85,-.235,.58,.38,-.668,-.127,.733],
  [-.77,-.82,.78,.33,.05,.20,.978],
  [-.825,-1.213,.951,.23,-.689,.318,.651],
  [-.840,1.192,.296,.17,-.955,-.280,.096],
  [1.451,-.539,.634,.43,-.053,.286,.957],
  [1.076,-.905,1.147,.23,-.243,.632,.736],
  [.914,-.533,.946,.24,-.605,.129,.786],
  [1.050,.315,.487,.24,.180,-.323,.929],
  [1.439,-1.129,1.188,.16,-.599,.225,.769],
  [-.155,-1.742,1.608,.15,-.852,-.231,.471],
]
for(let i=0;i<count;i++){
  normal.set(groups[groupOf[i]].n,i*3)
  const p=groups[groupOf[i]].p
  let bowl=0
  for(const [x,y,z,r,nx,ny,nz] of craters) {
    const dx=p[0]-x,dy=p[1]-y,dz=p[2]-z,depth=dx*nx+dy*ny+dz*nz
    const radial=Math.hypot(dx-depth*nx,dy-depth*ny,dz-depth*nz)
    const interior=(1-smooth(r*.65,r,radial))*smooth(-r*.35,-r*.10,depth)*(1-smooth(r*.45,r*.90,depth))
    bowl=Math.max(bowl,interior)
  }
  mask[i]=Math.max(cavity[groupOf[i]]*.18,bowl)
}
primitive.getAttribute('POSITION').setArray(position)
primitive.getAttribute('NORMAL').setArray(normal)
primitive.setAttribute('TANGENT',null)
primitive.setAttribute('_MOYAI_CAVITY',doc.createAccessor('Crater concavity').setType('SCALAR').setArray(mask).setBuffer(root.listBuffers()[0]))
const material=primitive.getMaterial()
const base=resolve(textures,'Meshy_AI_Silent_Stone_Orbit_1017192604_texture')
for(const [texture,path] of [[material.getBaseColorTexture(),base+'.png'],[material.getNormalTexture(),base+'_normal.png']]) {
  // FBX uses flipped image rows; match GLTFExporter's UV convention.
  texture.setImage(await sharp(await readFile(path)).flip().jpeg({quality:94,chromaSubsampling:'4:4:4'}).toBuffer()).setMimeType('image/jpeg')
}
const rough=await sharp(base+'_roughness.png').flip().greyscale().raw().toBuffer({resolveWithObject:true})
const metal=await sharp(base+'_metallic.png').flip().greyscale().raw().toBuffer()
const packed=Buffer.alloc(rough.info.width*rough.info.height*3)
for(let i=0;i<rough.data.length;i++){packed[i*3]=255;packed[i*3+1]=Math.max(190,rough.data[i]);packed[i*3+2]=metal[i]}
material.getMetallicRoughnessTexture().setImage(await sharp(packed,{raw:{width:rough.info.width,height:rough.info.height,channels:3}}).jpeg({quality:94,chromaSubsampling:'4:4:4'}).toBuffer()).setMimeType('image/jpeg')
material.setName('Weathered Silent Stone Orbit')
await doc.transform(prune())
await io.write(output,doc)
console.log(JSON.stringify({vertices:count,triangles:index.length/3,cavityVertices:mask.filter(v=>v>.1).length,maxCavity:Math.max(...mask)},null,2))
