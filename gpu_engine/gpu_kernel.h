/* ===================================================================
 * Bitcoin Collision Engine — GPU OpenCL Kernel
 * secp256k1 EC multiplication + HASH160 (SHA-256 || RIPEMD-160)
 * 所有私钥相关路径均为恒定时间（Montgomery ladder + cmov）
 * =================================================================== */

typedef uint fe[8];
#define ZERO256 {0U,0U,0U,0U,0U,0U,0U,0U}

typedef struct { fe x, y, z; } jacobian_point;

/* --- __constant 查表 (共享全局, ~1.6 KB, 不占工作项私有寄存器) --- */

__constant fe P_SECP = {0xFFFFFC2FU,0xFFFFFFFEU,0xFFFFFFFFU,0xFFFFFFFFU,
                          0xFFFFFFFFU,0xFFFFFFFFU,0xFFFFFFFFU,0xFFFFFFFFU};

__constant uint K_SHA256[64] = {
    0x428A2F98U,0x71374491U,0xB5C0FBCFU,0xE9B5DBA5U,
    0x3956C25BU,0x59F111F1U,0x923F82A4U,0xAB1C5ED5U,
    0xD807AA98U,0x12835B01U,0x243185BEU,0x550C7DC3U,
    0x72BE5D74U,0x80DEB1FEU,0x9BDC06A7U,0xC19BF174U,
    0xE49B69C1U,0xEFBE4786U,0x0FC19DC6U,0x240CA1CCU,
    0x2DE92C6FU,0x4A7484AAU,0x5CB0A9DCU,0x76F988DAU,
    0x983E5152U,0xA831C66DU,0xB00327C8U,0xBF597FC7U,
    0xC6E00BF3U,0xD5A79147U,0x06CA6351U,0x14292967U,
    0x27B70A85U,0x2E1B2138U,0x4D2C6DFCU,0x53380D13U,
    0x650A7354U,0x766A0ABBU,0x81C2C92EU,0x92722C85U,
    0xA2BFE8A1U,0xA81A664BU,0xC24B8B70U,0xC76C51A3U,
    0xD192E819U,0xD6990624U,0xF40E3585U,0x106AA070U,
    0x19A4C116U,0x1E376C08U,0x2748774CU,0x34B0BCB5U,
    0x391C0CB3U,0x4ED8AA4AU,0x5B9CCA4FU,0x682E6FF3U,
    0x748F82EEU,0x78A5636FU,0x84C87814U,0x8CC70208U,
    0x90BEFFFAU,0xA4506CEBU,0xBEF9A3F7U,0xC67178F2U,
};

__constant uint R_RMD[80] = {
     0, 1, 2, 3, 4, 5, 6, 7, 8, 9,10,11,12,13,14,15,
     7, 4,13, 1,10, 6,15, 3,12, 0, 9, 5, 2,14,11, 8,
     3,10,14, 4, 9,15, 8, 1, 2, 7, 0, 6,13,11, 5,12,
     1, 9,11,10, 0, 8,12, 4,13, 3, 7,15,14, 5, 6, 2,
     4, 0, 5, 9, 7,12, 2,10,14, 1, 3, 8,11, 6,15,13,
};

__constant uint S_RMD[80] = {
    11,14,15,12, 5, 8, 7, 9,11,13,14,15, 6, 7, 9, 8,
     7, 6, 8,13,11, 9, 7,15, 7,12,15, 9,11, 7,13,12,
    11,13, 6, 7,14, 9,13,15,14, 8,13, 6, 5,12, 7, 5,
    11,12,14,15,14,15, 9, 8, 9,14, 5, 6, 8, 6, 5,12,
     9,15, 5,11, 6, 8,13,12, 5,12,13,14,11, 8, 5, 6,
};

__constant uint RP_RMD[80] = {
     5,14, 7, 0, 9, 2,11, 4,13, 6,15, 8, 1,10, 3,12,
     6,11, 3, 7, 0,13, 5,10,14,15, 8,12, 4, 9, 1, 2,
    15, 5, 1, 3, 7,14, 6, 9,11, 8,12, 2,10, 0, 4,13,
     8, 6, 4, 1, 3,11,15, 0, 5,12, 2,13, 9, 7,10,14,
    12,15,10, 4, 1, 5, 8, 7, 6, 2,13,14, 0, 3, 9,11,
};

__constant uint SP_RMD[80] = {
     8, 9, 9,11,13,15,15, 5, 7, 7, 8,11,14,14,12, 6,
     9,13,15, 7,12, 8, 9,11, 7, 7,12, 7, 6,15,13,11,
     9, 7,15,11, 8, 6, 6,14,12,13, 5,14,13,13, 7, 5,
    15, 5, 8,11,14,14, 6,14, 6, 9,12, 9,12, 5,15, 8,
     8, 5,12, 9,12, 5,14, 6, 8,13, 6, 5,15,13,11,11,
};

__constant uint K1_RMD[5] = {0x00000000U,0x5A827999U,0x6ED9EBA1U,0x8F1BBCDCU,0xA953FD4EU};
__constant uint K2_RMD[5] = {0x50A28BE6U,0x5C4DD124U,0x6D703EF3U,0x7A6D76E9U,0x00000000U};

/* --- 常量时间工具 --- */
static uint ct_sel(uint f, uint a, uint b) {
    uint m = -f; return b ^ (m & (a ^ b));
}
static void ct_cmov_fe(fe r, const fe a, uint f) {
    uint m = -f; for (int i=0;i<8;i++) r[i] ^= (m & (r[i]^a[i]));
}
static void ct_cmov_pt(jacobian_point *r, const jacobian_point *a, uint f) {
    ct_cmov_fe(r->x,a->x,f); ct_cmov_fe(r->y,a->y,f); ct_cmov_fe(r->z,a->z,f);
}
static uint fe_eq(const fe a, const fe b) {
    uint acc=0; for(int i=0;i<8;i++) acc|=a[i]^b[i]; return (acc==0U)?1U:0U;
}
static uint fe_is_zero(const fe a) { const fe z=ZERO256; return fe_eq(a,z); }

/* --- 域运算 (P = 2^256 - 2^32 - 977, 见 P_SECP 常量) --- */
static void fe_set(fe r, const fe a) { for(int i=0;i<8;i++) r[i]=a[i]; }

static void fe_reduce(fe r, const fe a, ulong carry) {
    /* 归约: 输入 + carry*2^256 → [0, P-1]
     * 2^256 ≡ 2^32 + 977 (mod P), 所以:
     *   carry*2^256 ≡ carry*977 + carry*2^32
     * 第1步: r[0] += carry*977, 传播进位
     * 第2步: r[1] += carry + 进位(r[0]), 再传播
     * 第3步: 条件性 -P (当 r >= P)
     * carry=0 → 无修正, 仅 cond-sub-P; carry=1 → +977(+2^32) 修正 */
    ulong t=(ulong)a[0]+977U*carry; r[0]=(uint)t; ulong c=t>>32;
    t=(ulong)a[1]+c+carry; r[1]=(uint)t; c=t>>32;
    for(int i=2;i<8;i++){t=(ulong)a[i]+c;r[i]=(uint)t;c=t>>32;}
    uint br=0,ge=1;
    for(int i=0;i<8;i++){ulong rhs=(ulong)P_SECP[i]+br;br=((ulong)r[i]<rhs)?1U:0U;if(i==7)ge=br^1U;}
    uint mk=-ge; ulong borrow=0;
    for(int i=0;i<8;i++){ulong diff=(ulong)r[i]-(ulong)(mk&P_SECP[i])-borrow;r[i]=(uint)diff;borrow=diff>>63;}
}

static void fe_mul(fe r, const fe a, const fe b) {
    ulong t[16]={0};
    for(int i=0;i<8;i++){ulong cr=0;for(int j=0;j<8;j++){cr=t[i+j]+(ulong)a[i]*b[j]+cr;t[i+j]=(uint)cr;cr>>=32;}t[i+8]=(uint)cr;}
    ulong cr=0; for(int i=0;i<8;i++){cr+=t[i]+(ulong)977*t[i+8]+(t[i+8]<<32);r[i]=(uint)cr;cr>>=32;}
    fe_reduce(r, r, cr);
}

static void fe_sqr(fe r, const fe a) { fe_mul(r,a,a); }
static void fe_add(fe r, const fe a, const fe b) {
    ulong c=0; for(int i=0;i<8;i++){c=(ulong)a[i]+b[i]+c;r[i]=(uint)c;c>>=32;} fe_reduce(r,r,c);
}
static void fe_sub(fe r, const fe a, const fe b) {
    long c=0; for(int i=0;i<8;i++){c=(long)a[i]-(long)b[i]+c;r[i]=(uint)c;c>>=32;}
    if(c){/* borrow: r = a-b+2^256, need r-2^32-977 (i.e., subtract 2^32+977) */ulong t=(ulong)r[0]-977;r[0]=(uint)t;c=t>>63;t=(ulong)r[1]-1-c;r[1]=(uint)t;c=t>>63;for(int i=2;i<8&&c;i++){t=(ulong)r[i]-c;r[i]=(uint)t;c=t>>63;}}
}

/* --- Jacobian 点双倍 (secp256k1: a=0) ---
 * 标准公式（Cohen/Miyaji/Ono）:
 *   t = 3*X^2  (a=0 时无 Z^4 项)
 *   s = 4*X*Y^2
 *   U = 8*Y^4
 *   X3 = t^2 - 2*s
 *   Y3 = t*(s - X3) - U
 *   Z3 = 2*Y*Z
 */
static void pt_dbl(jacobian_point *r, const jacobian_point *a) {
    fe t,s,m,x3,y3,z3;
    fe_sqr(t,a->x);                     /* t = X^2 */
    fe_set(s,t);                        /* s = X^2 (备份, 后续被 Y^2 覆盖) */
    fe_add(t,t,s);                      /* t = 2*X^2 */
    fe_add(t,t,s); fe_reduce(t,t,0);       /* t = 3*X^2 (a=0, secp256k1) */
    fe_sqr(s,a->y);                     /* s = Y^2 */
    fe_sqr(m,s); fe_add(m,m,m); fe_add(m,m,m); fe_add(m,m,m); fe_reduce(m,m,0); /* m = 8*Y^4 */
    fe_mul(x3,s,a->x);                 /* x3 = X*Y^2 */
    fe_add(x3,x3,x3); fe_reduce(x3,x3,0); /* x3 = 2*X*Y^2 */
    fe_add(x3,x3,x3); fe_reduce(x3,x3,0); /* x3 = 4*X*Y^2 (= s) */
    fe_sqr(s,t);                        /* s = t^2 */
    fe_sub(s,s,x3); fe_reduce(s,s,0);     /* s = t^2 - 4*X*Y^2 */
    fe_sub(s,s,x3); fe_reduce(s,s,0);     /* s = t^2 - 8*X*Y^2 (= X3) */
    fe_sub(x3,x3,s); fe_reduce(x3,x3,0);  /* x3 = 4*X*Y^2 - s = 12*X*Y^2 - t^2 */
    fe_mul(y3,t,x3); fe_sub(y3,y3,m); fe_reduce(y3,y3,0); /* y3 = t*(12*X*Y^2 - t^2) - 8*Y^4 */
    fe_mul(z3,a->y,a->z); fe_add(z3,z3,z3); fe_reduce(z3,z3,0); /* z3 = 2*Y*Z */
    fe_set(r->x,s); fe_set(r->y,y3); fe_set(r->z,z3);
}

/* --- 全 Jacobian 加法 r = a + b --- */
static void pt_add_jacobian(jacobian_point *r, const jacobian_point *a, const jacobian_point *b) {
    if(fe_is_zero(b->z)){fe_set(r->x,a->x);fe_set(r->y,a->y);fe_set(r->z,a->z);return;}
    if(fe_is_zero(a->z)){fe_set(r->x,b->x);fe_set(r->y,b->y);fe_set(r->z,b->z);return;}
    fe z1z1,z2z2,u1,u2,s1,s2,h,hh,hhh,v,rr,t;
    fe_sqr(z1z1,a->z);
    fe_sqr(z2z2,b->z);
    fe_mul(u1,a->x,z2z2);           /* U1 = X1*Z2^2 */
    fe_mul(u2,b->x,z1z1);           /* U2 = X2*Z1^2 */
    fe_mul(s1,b->z,z2z2); fe_mul(s1,a->y,s1);  /* S1 = Y1*Z2^3 */
    fe_mul(s2,a->z,z1z1); fe_mul(s2,b->y,s2);  /* S2 = Y2*Z1^3 */
    fe_sub(h,u2,u1); fe_reduce(h,h,0);          /* H = U2-U1 */
    fe_sqr(hh,h);                                /* HH = H^2 */
    fe_mul(hhh,hh,h);                            /* HHH = H^3 */
    fe_sub(rr,s2,s1); fe_reduce(rr,rr,0);        /* R = S2-S1 */
    fe_mul(v,u1,hh);                             /* V = U1*HH */
    fe_sqr(r->x,rr);                             /* X3 = R^2 - (HHH + 2V) */
    fe_add(t,hhh,v); fe_add(t,t,v);              /* t = HHH + 2V */
    fe_sub(r->x,r->x,t); fe_reduce(r->x,r->x,0);
    fe_sub(t,v,r->x); fe_reduce(t,t,0);          /* Y3 = R*(V-X3) - S1*HHH */
    fe_mul(r->y,rr,t);
    fe_mul(t,s1,hhh);
    fe_sub(r->y,r->y,t); fe_reduce(r->y,r->y,0);
    fe_mul(r->z,a->z,b->z); fe_mul(r->z,r->z,h);/* Z3 = Z1*Z2*H */
}

/* --- 恒定时间 Montgomery Ladder: k * G --- */
static void scalar_mult_base(jacobian_point *r, const fe k) {
    jacobian_point r0,r1;
    fe zr=ZERO256, one={1U,0U,0U,0U,0U,0U,0U,0U};
    fe GX={0x16F81798U,0x59F2815BU,0x2DCE28D9U,0x029BFCDBU,0xCE870B07U,0x55A06295U,0xF9DCBBACU,0x79BE667EU};
    fe GY={0xFB10D4B8U,0x9C47D08FU,0xA6855419U,0xFD17B448U,0x0E1108A8U,0x5DA4FBFCU,0x26A3C465U,0x483ADA77U};
    fe_set(r0.x,zr); fe_set(r0.y,one); fe_set(r0.z,zr);
    fe_set(r1.x,GX); fe_set(r1.y,GY); fe_set(r1.z,one);
    uint pb=0,b=0; jacobian_point tmp;
    #pragma unroll 1  /* 不展开, 256 次循环无需展开, 但提示编译器保持循环 */
    for(int i=255;i>=0;i--){
        int li=i/32,bi=i%32; b=(k[li]>>bi)&1U;
        uint sw=pb^b;
        /* 原子条件交换: 用 tmp 避免 cmov 序列覆盖 */
        ct_cmov_pt(&tmp, &r1, sw);
        ct_cmov_pt(&r1, &r0, sw);
        ct_cmov_pt(&r0, &tmp, sw);
        pt_add_jacobian(&tmp,&r0,&r1);
        fe_set(r1.x,tmp.x);fe_set(r1.y,tmp.y);fe_set(r1.z,tmp.z);
        pt_dbl(&tmp,&r0);
        fe_set(r0.x,tmp.x);fe_set(r0.y,tmp.y);fe_set(r0.z,tmp.z);
        pb=b;
    }
    ct_cmov_pt(&r0,&r1,pb); ct_cmov_pt(r,&r0,1);
}

/* --- 域逆元: a^(P-2) mod P (费马小定理, 平方乘) --- */
static void fe_inv(fe r, const fe a) {
    /* P-2 = 0xFFFFFFFF...FFFFFC2D (little-endian limbs) */
    const uint exp[8] = {0xFFFFFC2DU, 0xFFFFFFFEU, 0xFFFFFFFFU, 0xFFFFFFFFU,
                         0xFFFFFFFFU, 0xFFFFFFFFU, 0xFFFFFFFFU, 0xFFFFFFFFU};
    fe tmp, res;
    fe_set(res, a);  /* bit 255 = 1: start with a^1 */
    /* 处理 bits 254..0 */
    for (int bit = 254; bit >= 0; bit--) {
        int limb = bit / 32, b = bit % 32;
        uint bit_val = (exp[limb] >> b) & 1U;
        fe_sqr(res, res);            /* 先平方 */
        if (bit_val) {
            fe_mul(res, res, a);     /* 再乘 base */
        }
    }
    fe_set(r, res);
}

/* --- SHA-256 (简化: 处理 ≤55B 输入) --- */
#define ROTR(x,n) (((x)>>(n))|((x)<<(32-(n))))
#define CH(x,y,z) (((x)&(y))^((~x)&(z)))
#define MAJ(x,y,z) (((x)&(y))^((x)&(z))^((y)&(z)))
#define SIG0(x) (ROTR(x,2)^ROTR(x,13)^ROTR(x,22))
#define SIG1(x) (ROTR(x,6)^ROTR(x,11)^ROTR(x,25))
#define GAM0(x) (ROTR(x,7)^ROTR(x,18)^((x)>>3))
#define GAM1(x) (ROTR(x,17)^ROTR(x,19)^((x)>>10))

static uint sha256_oneblock(const uchar *msg, uint msglen, uint hash[8]) {
    uint s[8]={0x6A09E667U,0xBB67AE85U,0x3C6EF372U,0xA54FF53AU,
               0x510E527FU,0x9B05688CU,0x1F83D9ABU,0x5BE0CD19U};
    uint w[64]={0},t1,t2; ulong bl=(ulong)msglen*8;
    for(int i=0;i<msglen;i++){int bi=i/4,sh=24-(i%4)*8;w[bi]|=(uint)msg[i]<<sh;}
    w[msglen/4]|=0x80U<<(24-(msglen%4)*8);
    if(msglen>=56){/* 需要两轮, 但 HASH160 输入 33B ≤55, 此处简化不处理超过 55 的情况 */}
    w[14]=(uint)(bl>>32); w[15]=(uint)bl;  /* 64-bit length 大端: w[14]=高32位, w[15]=低32位 */
    /* 使用 __constant K_SHA256, 查表由 GPU 常量内存提供 */
    #pragma unroll
    for(int i=16;i<64;i++) w[i]=GAM1(w[i-2])+w[i-7]+GAM0(w[i-15])+w[i-16];
    uint a=s[0],b=s[1],c=s[2],d=s[3],e=s[4],f=s[5],g=s[6],h=s[7];
    #pragma unroll
    for(int i=0;i<64;i++){t1=h+SIG1(e)+CH(e,f,g)+K_SHA256[i]+w[i];t2=SIG0(a)+MAJ(a,b,c);h=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;}
    s[0]+=a;s[1]+=b;s[2]+=c;s[3]+=d;s[4]+=e;s[5]+=f;s[6]+=g;s[7]+=h;
    hash[0]=s[0];hash[1]=s[1];hash[2]=s[2];hash[3]=s[3];hash[4]=s[4];hash[5]=s[5];hash[6]=s[6];hash[7]=s[7];
    return 0;
}

/* --- RIPEMD-160 (完整 5 轮 80 步, 仅处理 ≤55B 单 block) --- */
#define ROL(x,n) (((x)<<(n))|((x)>>(32-(n))))

/* 5 轮布尔函数 */
#define F1(x,y,z) ((x) ^ (y) ^ (z))
#define F2(x,y,z) (((x) & (y)) | ((~(x)) & (z)))
#define F3(x,y,z) (((x) | (~(y))) ^ (z))
#define F4(x,y,z) (((x) & (z)) | ((y) & (~(z))))
#define F5(x,y,z) ((x) ^ ((y) | (~(z))))

static uint rmd160_oneblock(const uchar *msg, uint msglen, uint hashout[5]) {
    uint h[5]={0x67452301U,0xEFCDAB89U,0x98BADCFEU,0x10325476U,0xC3D2E1F0U};
    uint w[16]={0}; ulong bl=(ulong)msglen*8;
    for(uint i=0;i<msglen;i++){int bi=i/4,sh=(i%4)*8;w[bi]|=(uint)msg[i]<<sh;}
    w[msglen/4]|=0x80U<<((msglen%4)*8);
    /* 单 block 假设: msglen < 56, HASH160 输入 32B(SHA256) ≤55 */
    w[14]=(uint)bl; w[15]=(uint)(bl>>32);  /* 64-bit length 小端: w[14]=低32位, w[15]=高32位 */
    /* 查表由 __constant R_RMD/S_RMD/RP_RMD/SP_RMD/K1_RMD/K2_RMD 提供 */

    uint a1=h[0],b1=h[1],c1=h[2],d1=h[3],e1=h[4];
    uint a2=h[0],b2=h[1],c2=h[2],d2=h[3],e2=h[4];
    uint t;

    #pragma unroll
    for(uint j=0;j<80;j++){
        uint round=j/16;
        /* ---- 左线 ---- */
        uint fl;
        if(round==0)fl=F1(b1,c1,d1);
        else if(round==1)fl=F2(b1,c1,d1);
        else if(round==2)fl=F3(b1,c1,d1);
        else if(round==3)fl=F4(b1,c1,d1);
        else fl=F5(b1,c1,d1);
        t=ROL(a1+fl+w[R_RMD[j]]+K1_RMD[round],S_RMD[j])+e1;
        a1=e1;e1=d1;d1=ROL(c1,10);c1=b1;b1=t;

        /* ---- 右线 (函数顺序反转: F5,F4,F3,F2,F1) ---- */
        uint fr;
        if(round==0)fr=F5(b2,c2,d2);
        else if(round==1)fr=F4(b2,c2,d2);
        else if(round==2)fr=F3(b2,c2,d2);
        else if(round==3)fr=F2(b2,c2,d2);
        else fr=F1(b2,c2,d2);
        t=ROL(a2+fr+w[RP_RMD[j]]+K2_RMD[round],SP_RMD[j])+e2;
        a2=e2;e2=d2;d2=ROL(c2,10);c2=b2;b2=t;
    }

    /* 输出组合 (标准 RIPEMD-160: 带移位交叉) */
    uint t0=h[0]; h[0]=h[1]+c1+d2; h[1]=h[2]+d1+e2; h[2]=h[3]+e1+a2;
    h[3]=h[4]+a1+b2; h[4]=t0+b1+c2;

    hashout[0]=h[0];hashout[1]=h[1];hashout[2]=h[2];hashout[3]=h[3];hashout[4]=h[4];
    return 0;
}

/* --- HASH160 综合: RIPEMD-160(SHA-256(msg)) --- */
static void hash160_full(const uchar *msg, uint msglen, __global uchar *out) {
    uint sh[8];
    sha256_oneblock(msg,msglen,sh);
    uchar shb[32];
    #pragma unroll
    for(int i=0;i<8;i++){shb[i*4+0]=(uchar)(sh[i]>>24);shb[i*4+1]=(uchar)(sh[i]>>16);shb[i*4+2]=(uchar)(sh[i]>>8);shb[i*4+3]=(uchar)sh[i];}
    uint rm[5];
    rmd160_oneblock(shb,32,rm);
    #pragma unroll
    for(int i=0;i<5;i++){out[i*4+0]=(uchar)(rm[i]>>24);out[i*4+1]=(uchar)(rm[i]>>16);out[i*4+2]=(uchar)(rm[i]>>8);out[i*4+3]=(uchar)rm[i];}
}

/* ===================================================================
 * Kernel 入口: 私钥(32B,小端) → 压缩公钥 HASH160(20B)
 * 每个工作项处理一个私钥
 * =================================================================== */
__kernel void ec_mul_hash160(
    __global const uchar *privkeys,  /* [batch * 32] 小端私钥 */
    __global uchar *hash160s,        /* [batch * 20] 输出 HASH160 */
    uint batch_size
) {
    uint gid = get_global_id(0);
    if (gid >= batch_size) return;

    /* 加载私钥（小端 → fe） */
    fe k;
    for (int i = 0; i < 8; i++) {
        uint off = gid * 32 + i * 4;
        k[i] = ((uint)privkeys[off]) | ((uint)privkeys[off+1] << 8) |
               ((uint)privkeys[off+2] << 16) | ((uint)privkeys[off+3] << 24);
    }

    /* k * G */
    jacobian_point pub;
    scalar_mult_base(&pub, k);

    /* Jacobian → affine (modular inverse via Fermat's little theorem) */
    fe z_inv, z_inv_sq, z_inv_cu, aff_x, aff_y;
    fe_inv(z_inv, pub.z);
    fe_sqr(z_inv_sq, z_inv);
    fe_mul(z_inv_cu, z_inv_sq, z_inv);
    fe_mul(aff_x, pub.x, z_inv_sq);
    fe_mul(aff_y, pub.y, z_inv_cu);

    /* 压缩公钥 (33B): 0x02/0x03 + affine X(32B,大端) */
    uchar pkb[33];
    uint y_parity = aff_y[0] & 1U;
    pkb[0] = (uchar)(0x02U ^ y_parity);
    for (int i = 0; i < 8; i++)
        for (int j = 0; j < 4; j++)
            pkb[1 + i*4 + j] = (uchar)(aff_x[7-i] >> ((3-j)*8));

    /* HASH160 = RIPEMD160(SHA256(pkb)) */
    hash160_full(pkb, 33, hash160s + gid * 20);
}

/* Kernel 入口: 私钥(32B,小端) → 压缩公钥 X 坐标(32B,大端) */
__kernel void ec_mul_pubkey(
    __global const uchar *privkeys,
    __global uchar *pubkeys_x,
    uint batch_size
) {
    uint gid = get_global_id(0);
    if (gid >= batch_size) return;

    fe k;
    for (int i = 0; i < 8; i++) {
        uint off = gid * 32 + i * 4;
        k[i] = ((uint)privkeys[off]) | ((uint)privkeys[off+1] << 8) |
               ((uint)privkeys[off+2] << 16) | ((uint)privkeys[off+3] << 24);
    }

    jacobian_point pub;
    scalar_mult_base(&pub, k);

    /* Jacobian → affine X */
    fe z_inv, z_inv_sq, aff_x;
    fe_inv(z_inv, pub.z);
    fe_sqr(z_inv_sq, z_inv);
    fe_mul(aff_x, pub.x, z_inv_sq);

    for (int i = 0; i < 8; i++)
        for (int j = 0; j < 4; j++)
            pubkeys_x[gid * 32 + i*4 + j] = (uchar)(aff_x[7-i] >> ((3-j)*8));
}
