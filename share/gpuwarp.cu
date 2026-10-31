// lmap GPU warp kernel: GDAL's 'average' resampling (gdalwarpkernel.cpp GWKAverageOrModeThread, GDAL 3.10)
// for a same-CRS warp, 3 x 8-bit bands. Per output pixel the host gives the source span on each axis
// ([lo,hi] in source pixels, index range [i0,i1)); the kernel applies GDAL's edge weights, its incremental
// weighted mean and its unsigned rounding ((int)(v + 0.5)). Build: nvcc -O3 -fmad=false -shared -Xcompiler -fPIC
// (-fmad=false: no fused multiply-add, so the double maths rounds exactly like the CPU).
extern "C" __global__ void avg(const unsigned char* src, int sw,
    const double* xl, const double* xh, const int* xi0, const int* xi1,
    const double* yl, const double* yh, const int* yi0, const int* yi1,
    unsigned char* dst, int W, int H)
{
    int x = blockIdx.x * blockDim.x + threadIdx.x;
    int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= W || y >= H) return;
    const int X0 = xi0[x], X1 = xi1[x], Y0 = yi0[y], Y1 = yi1[y];
    const double XL = xl[x], XH = xh[x], YL = yl[y], YH = yh[y];
    double v0 = 0, v1 = 0, v2 = 0, tw = 0;
    for (int sy = Y0; sy < Y1; sy++) {
        double wy = (sy == Y0) ? ((Y0 + 1 == Y1) ? 1.0 : 1 - (YL - Y0))
                  : (sy + 1 == Y1) ? 1 - (Y1 - YH) : 1.0;
        const unsigned char* row = src + (size_t)sy * sw * 3;
        for (int sx = X0; sx < X1; sx++) {
            double w = (sx == X0) ? ((X0 + 1 == X1) ? wy : wy * (1 - (XL - X0)))
                     : (sx + 1 == X1) ? wy * (1 - (X1 - XH)) : wy;
            if (w > 0) {
                tw += w; double f = w / tw;
                const unsigned char* p = row + (size_t)sx * 3;
                v0 += f * ((double)p[0] - v0);
                v1 += f * ((double)p[1] - v1);
                v2 += f * ((double)p[2] - v2);
            }
        }
    }
    unsigned char* o = dst + ((size_t)y * W + x) * 3;      // GDAL: (GByte)(v + 0.5); clamp only guards ulp drift
    o[0] = (unsigned char)(fmin(fmax(v0, 0.0), 255.0) + 0.5);
    o[1] = (unsigned char)(fmin(fmax(v1, 0.0), 255.0) + 0.5);
    o[2] = (unsigned char)(fmin(fmax(v2, 0.0), 255.0) + 0.5);
}

template <class T> static int up(T** d, const T* h, size_t n)
{
    if (cudaMalloc(d, n * sizeof(T)) != cudaSuccess) return 1;
    return cudaMemcpy(*d, h, n * sizeof(T), cudaMemcpyHostToDevice) != cudaSuccess;
}

// returns 0 on success, else a CUDA error code (the caller falls back to gdalwarp)
extern "C" int run(const unsigned char* src, int sw, int sh,
    const double* xl, const double* xh, const int* xi0, const int* xi1,
    const double* yl, const double* yh, const int* yi0, const int* yi1,
    unsigned char* dst, int W, int H)
{
    unsigned char *ds = 0, *dd = 0; double *a = 0, *b = 0, *c = 0, *d = 0; int *e = 0, *f = 0, *g = 0, *h = 0;
    int rc = 0;
    if (up(&ds, src, (size_t)sw * sh * 3) || cudaMalloc(&dd, (size_t)W * H * 3) != cudaSuccess
        || up(&a, xl, W) || up(&b, xh, W) || up(&c, yl, H) || up(&d, yh, H)
        || up(&e, xi0, W) || up(&f, xi1, W) || up(&g, yi0, H) || up(&h, yi1, H)) {
        rc = (int)cudaGetLastError(); if (!rc) rc = -1;
    } else {
        dim3 bl(32, 8), gr((W + 31) / 32, (H + 7) / 8);
        avg<<<gr, bl>>>(ds, sw, a, b, e, f, c, d, g, h, dd, W, H);
        rc = (int)cudaGetLastError();
        if (!rc) rc = (int)cudaMemcpy(dst, dd, (size_t)W * H * 3, cudaMemcpyDeviceToHost);
    }
    cudaFree(ds); cudaFree(dd); cudaFree(a); cudaFree(b); cudaFree(c); cudaFree(d);
    cudaFree(e); cudaFree(f); cudaFree(g); cudaFree(h);
    return rc;
}
