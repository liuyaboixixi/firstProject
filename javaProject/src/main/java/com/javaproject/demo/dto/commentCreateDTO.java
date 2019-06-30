package com.javaproject.demo.dto;

import lombok.Data;

@Data
public class commentCreateDTO {
    private long parentId;
    private String content;
    private Integer type;
}
